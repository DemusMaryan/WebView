"""One opt-in HTTPS three-video acceptance; no retries, builds or production fixtures."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import ssl
import time
from uuid import uuid4
import httpx
from sqlalchemy import text
from mtw.config import CV_ROOT, DATA, LIMITS, PORTS, TERMINAL, job_dir
from mtw.capacity import ensure_space
from mtw.db import engine
from mtw.files import atomic_json, read_json
from mtw.metrics import process_sample
from mtw.processes import identity, group_processes
from mtw import tls
from parallel_e2e import signature
from check_parallel_results import validate


def utc():return datetime.now(timezone.utc).isoformat()


def rows():
    with engine.connect() as c:return {r['id']:r for r in c.execute(text('SELECT row_to_json(jobs) FROM jobs')).scalars()}


def users_digest():
    with engine.connect() as c:users=list(c.execute(text('SELECT row_to_json(users) FROM users ORDER BY id')).scalars())
    return hashlib.sha256(json.dumps(users,sort_keys=True).encode()).hexdigest()


def prepare():
    ensure_space();saved=rows()
    assert all(j['status'] in TERMINAL for j in saved.values()),'Existing work active; do not interrupt it'
    assert len(saved)+3<=LIMITS['stored_jobs'],'Not enough stored-job capacity for exactly three new jobs'
    root=DATA/'reports'/('slot-balancing-'+uuid4().hex);root.mkdir(mode=0o700)
    prior=read_json(DATA/'reports/admin-14da9ecf049443b5b1dbe6f97504205d/before.json')
    core=prior['core'];proofs=prior['proofs']
    assert all(hashlib.sha256((CV_ROOT/p).read_bytes()).hexdigest()==h for p,h in core.items())
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in proofs.items())
    atomic_json(root/'before.json',{'at':utc(),'rows':saved,'users_digest':users_digest(),
        'files':{j:signature(job_dir(j)) for j in saved},'core':core,'proofs':proofs,
        'C_free_bytes':shutil.disk_usage('/mnt/c').free,'native_free_bytes':shutil.disk_usage(DATA).free})
    print(json.dumps({'root':str(root),'saved_jobs':len(saved),'C_free_bytes':shutil.disk_usage('/mnt/c').free}))


def process(pid):
    sample=process_sample(pid) if pid else None
    return {k:sample[k] for k in ('host_pid','cpu_ticks','rss_bytes','start_ticks','pid_namespace','container_pid') if k in sample} if sample else None


def execute(root):
    before=read_json(root/'before.json');ensure_space()
    assert rows()==before['rows'],'Production jobs changed after preflight; inspect before accepting new work'
    with (root/'attempted').open('x') as f:f.write(utc())
    base=tls.canonical_origin()
    client=httpx.Client(base_url=base,verify=ssl.create_default_context(cafile=tls.paths()['ca_cert']),timeout=15,trust_env=False)
    report={'started_at':utc(),'passed':False,'jobs':{},'http':[],'observations':[]}
    start=time.monotonic();log=DATA/'logs/dispatcher.log';nginx=DATA/'logs/nginx-access.log'
    log_offset=log.stat().st_size;nginx_offset=nginx.stat().st_size
    def save():atomic_json(root/'run.json',report)
    def request(method,path,**kwargs):
        at=time.monotonic();response=client.request(method,path,**kwargs)
        report['http'].append({'at':utc(),'path':path,'method':method,'http':response.status_code,'seconds':time.monotonic()-at})
        response.raise_for_status();return response
    def upload(label,seconds):
        ensure_space();video=CV_ROOT/f'datasets/freiburg1_xyz-rgb-30fps-short-{seconds}s.mp4'
        with video.open('rb') as f:
            response=request('POST','/api/jobs',files={'video':(video.name,f,'video/mp4')},
                data={'parameters':video.with_suffix('.parameters.json').read_text()})
        assert response.status_code==202
        job=response.json();report['jobs'][label]={'id':job['id'],'frames':seconds*30};save()
    def observe(label):
        job_id=report['jobs'][label]['id'];state=request('GET',f'/api/jobs/{job_id}').json()
        worker=read_json(job_dir(job_id)/'worker-process.json',{});native=read_json(job_dir(job_id)/'child.json',{})
        sample={k:state[k] for k in ('status','server_id','stage','started_at','finished_at','processed_frames','worker_pid')}
        sample.update(worker=process(worker.get('pid')),native=process(native.get('pid')))
        snap=request('GET',f'/api/jobs/{job_id}/snapshot').json()
        assert snap['job_id']==job_id
        sample['snapshot']={k:snap.get(k) for k in ('frame_id','tracking_state','processed_frames','server_id')}
        return sample
    try:
        bootstrap=request('GET','/api/auth/session').json()
        client.headers.update({'Origin':base,'X-CSRF-Token':bootstrap['csrf_token']})
        credentials=read_json(DATA/'reports/queue-356124ce0fb14080873fd255b3fbec2f/credentials.json')
        auth=request('POST','/api/auth/login',json=credentials).json();client.headers['X-CSRF-Token']=auth['csrf_token']
        # No demand yet: exactly the two baseline ready servers, no nspawn launch.
        with engine.connect() as c:
            ready=list(c.execute(text("SELECT server_id FROM compute_servers WHERE state='ready' ORDER BY server_id")).scalars())
        assert ready==['compute-1','compute-2'],ready
        upload('A',10);upload('B',12)
        terminal={};submitted=False;deadline=time.monotonic()+240
        with (root/'series.jsonl').open('w') as series:
            while time.monotonic()<deadline:
                ensure_space();sample={'at':utc(),'elapsed':time.monotonic()-start,'C_free_bytes':shutil.disk_usage('/mnt/c').free,'jobs':{}}
                for label in list(report['jobs']):
                    if label not in terminal:
                        value=observe(label);sample['jobs'][label]=value
                        if value['status'] in TERMINAL:terminal[label]=value
                series.write(json.dumps(sample)+'\n');series.flush()
                report['observations'].append(sample)
                if not submitted:
                    completed=[k for k in ('A','B') if k in terminal]
                    if len(completed)==2:raise AssertionError('Both native jobs finished before third submission; no automatic retry')
                    if len(completed)==1:
                        first=completed[0];other='B' if first=='A' else 'A'
                        live=sample['jobs'].get(other)
                        assert terminal[first]['status']=='succeeded',terminal[first]
                        assert live and live['status']=='running' and live['native'] and live['processed_frames']>0,live
                        report['free_server_event']={'at':utc(),'freed':terminal[first]['server_id'],
                            'finished_job':report['jobs'][first]['id'],'neighbour_job':report['jobs'][other]['id'],'neighbour':live}
                        upload('C',10);submitted=True
                save()
                if len(terminal)==3:break
                time.sleep(.2)
        assert len(terminal)==3,'Acceptance observation deadline exceeded'
        assert all(j['status']=='succeeded' for j in terminal.values()),terminal
        assert terminal['A']['server_id']!=terminal['B']['server_id']
        assert terminal['C']['server_id']==report['free_server_event']['freed']
        decisions=[]
        with log.open() as f:
            f.seek(log_offset)
            for line in f:
                try:value=json.loads(line)
                except ValueError:continue
                if value.get('event')=='scheduler_decision' and value.get('job_id') in {j['id'] for j in report['jobs'].values()}:decisions.append(value)
        selected=next(d for d in decisions if d['job_id']==report['jobs']['C']['id'] and d['server_id'])
        neighbour=report['free_server_event']['neighbour']['server_id']
        assert next(c for c in selected['candidates'] if c['server_id']==neighbour)['running']==1
        assert next(c for c in selected['candidates'] if c['server_id']==selected['server_id'])['load']==0
        atomic_json(root/'scheduler-decisions.json',decisions);report['third_decision']=selected
        with nginx.open() as f:f.seek(nginx_offset);routes=[line.strip() for line in f if any(j['id'] in line for j in report['jobs'].values()) and '/internal/compute/' in line]
        for label,j in report['jobs'].items():
            server=terminal[label]['server_id'];port=PORTS[server.replace('-','_')]
            assert any(j['id'] in line and f'/internal/compute/{server}/execute' in line and f'upstream=127.0.0.1:{port}' in line and f'server={server}' in line for line in routes)
        atomic_json(root/'nginx-routes.json',routes)
        validations={}
        for label,j in report['jobs'].items():
            validations[label]=validate(client,j['id'],j['frames'])
            proof=read_json(job_dir(j['id'])/'termination.json');assert proof['group_members_after']==[]
            assert not group_processes(proof['worker_pid']) and identity(proof['worker_pid']) is None
            assert not (job_dir(j['id'])/'sequence/rgb').exists()
            snap=request('GET',f"/api/jobs/{j['id']}/snapshot").json();assert request('GET',snap['frame_url']).status_code==200
        atomic_json(root/'validation.json',validations)
        overlap=[s for s in report['observations'] if all(s['jobs'].get(k,{}).get('native') for k in ('A','B'))]
        assert len(overlap)>=2
        for label in ('A','B'):
            assert overlap[-1]['jobs'][label]['native']['cpu_ticks']>overlap[0]['jobs'][label]['native']['cpu_ticks']
            assert overlap[-1]['jobs'][label]['processed_frames']>overlap[0]['jobs'][label]['processed_frames']
        report['native_overlap_observed_seconds']=overlap[-1]['elapsed']-overlap[0]['elapsed']
        report['native_overlap_first_last']=[overlap[0],overlap[-1]]
        after=rows();checks={'old_rows_owners':all(after[j]==v for j,v in before['rows'].items()),
            'old_files':all(signature(job_dir(j))==v for j,v in before['files'].items()),
            'users':users_digest()==before['users_digest'],
            'core':all(hashlib.sha256((CV_ROOT/p).read_bytes()).hexdigest()==h for p,h in before['core'].items()),
            'proofs':all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in before['proofs'].items()),
            'exactly_three_new':set(after)-set(before['rows'])=={j['id'] for j in report['jobs'].values()}}
        assert all(checks.values()),checks
        report.update(passed=True,checks=checks,job_count=len(after),results={k:v['result'] for k,v in validations.items()})
    except BaseException as error:
        report['error']=type(error).__name__+': '+str(error)
        # Only this attempt's jobs, never previous user work; no automatic retry.
        for job in report['jobs'].values():
            try:
                if request('GET',f"/api/jobs/{job['id']}").json()['status'] not in TERMINAL:request('POST',f"/api/jobs/{job['id']}/cancel")
            except Exception:pass
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            states={j['id']:client.get(f"/api/jobs/{j['id']}").json() for j in report['jobs'].values()}
            if all(s.get('status') in TERMINAL for s in states.values()):
                report['failed_attempt_cleanup']={j:s['status'] for j,s in states.items()};break
            time.sleep(.1)
        raise
    finally:
        report.update(finished_at=utc(),C_free_bytes=shutil.disk_usage('/mnt/c').free);save()
        try:client.post('/api/auth/logout')
        finally:client.close()
    print(json.dumps({k:report[k] for k in ('passed','jobs','results','checks','native_overlap_observed_seconds','C_free_bytes')}))


if __name__=='__main__':
    os.umask(0o077)
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('prepare','run'));parser.add_argument('--root',type=Path)
    args=parser.parse_args()
    if args.action=='prepare':prepare()
    else:
        root=args.root.resolve();assert root.parent==(DATA/'reports').resolve() and root.name.startswith('slot-balancing-')
        execute(root)
