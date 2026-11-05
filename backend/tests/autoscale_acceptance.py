"""Explicit R7 evidence helpers. Preparing/observing never creates SLAM jobs."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import ssl
import sys
import time
from uuid import uuid4
import httpx
from sqlalchemy import text,select
from mtw.config import CV_ROOT,DATA,TERMINAL,job_dir,LIMITS
from mtw.capacity import ensure_space
from mtw.db import engine,ComputeServer,Session
from mtw.files import read_json,atomic_json
from mtw import container_runtime as runtime,tls
from mtw.processes import identity
from parallel_e2e import signature
from check_parallel_results import validate


def utc(): return datetime.now(timezone.utc).isoformat()


def rows():
    with engine.connect() as c: return {r['id']:r for r in c.execute(text('SELECT row_to_json(jobs) FROM jobs')).scalars()}


def private_json(path,value):
    with os.fdopen(os.open(path,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600),'w') as out: json.dump(value,out)


def prepare(expected_jobs=54):
    ensure_space()
    if shutil.disk_usage('/mnt/c').free<10*2**30: raise ValueError('Initial C: free below 10 GiB')
    before=rows()
    assert len(before)==expected_jobs and expected_jobs>=54 and all(j['status'] in TERMINAL for j in before.values())
    source=read_json(DATA/'reports/queue-356124ce0fb14080873fd255b3fbec2f/before.json')
    core={n:hashlib.sha256((CV_ROOT/n).read_bytes()).hexdigest() for n in source['cv_hashes']}
    r5={n:hashlib.sha256((DATA/'reports'/n).read_bytes()).hexdigest() for n in source['proofs']}
    assert core==source['cv_hashes'] and r5==source['proofs']
    r6root=DATA/'reports/queue-356124ce0fb14080873fd255b3fbec2f'
    r6={n:hashlib.sha256((r6root/n).read_bytes()).hexdigest() for n in ('before.json','browser.json','validation.json','queue-timing.json','native-series.jsonl')}
    with engine.connect() as c:
        users=list(c.execute(text('SELECT row_to_json(users) FROM users ORDER BY id')).scalars())
    root=DATA/'reports'/('autoscale-'+uuid4().hex);root.mkdir(mode=0o700)
    atomic_json(root/'before.json',{'at':utc(),'jobs':before,'files':{j:signature(job_dir(j)) for j in before},
        'users_sha256':hashlib.sha256(json.dumps(users,sort_keys=True).encode()).hexdigest(),
        'core':core,'r5':r5,'r6':r6,'C_free_bytes':shutil.disk_usage('/mnt/c').free,
        'native_free_bytes':shutil.disk_usage(DATA).free,'nginx_log_offset':(DATA/'logs/nginx-access.log').stat().st_size,
        'autoscaler_log_offset':(DATA/'logs/autoscaler.log').stat().st_size if (DATA/'logs/autoscaler.log').exists() else 0})
    # Reuse the explicitly test-only R6 account. No reassignment of the legacy 51.
    credentials=read_json(r6root/'credentials.json')
    private_json(root/'credentials.json',credentials)
    print(json.dumps({'root':str(root),'saved_jobs':len(before),'C_free_bytes':shutil.disk_usage('/mnt/c').free}))


def client(root):
    secret=read_json(root/'session.json')
    return httpx.Client(base_url=tls.canonical_origin(),verify=ssl.create_default_context(cafile=tls.paths()['ca_cert']),
        cookies={'mtw_session':secret['cookie']},headers={'Origin':tls.canonical_origin(),'X-CSRF-Token':secret['csrf']},timeout=30,trust_env=False)


def registry():
    with Session() as s:
        return {r.server_id:{'state':r.state,'attempts':r.attempts,'process':r.process,'error':r.error} for r in s.scalars(select(ComputeServer))}


def process(pid):
    item=identity(pid)
    if not item: return None
    try:
        stat=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
        return item|{'cpu_ticks':int(stat[11])+int(stat[12]),'rss_bytes':int(stat[21])*os.sysconf('SC_PAGE_SIZE')}
    except (FileNotFoundError,ProcessLookupError): return None


def observe(root):
    start=time.monotonic()
    try:
        with (root/'series.jsonl').open('x',buffering=1) as out:
            while time.monotonic()-start<600:
                ensure_space()
                current=rows();servers=registry();jobs=read_json(root/'jobs.json',{})
                active=[r for r in current.values() if r['status'] in ('running','dispatching')]
                assert len(active)<=3
                busy=[r['server_id'] for r in active if r['server_id']]
                assert len(busy)==len(set(busy))
                launches=runtime.owned_launches()
                assert len(launches)<=1,{'unexpected_supervisors':launches}
                namespace=(servers['compute-3']['process'] or {}).get('pid_namespace')
                inside=runtime.namespace_members(namespace)
                sample={'at':utc(),'monotonic':time.monotonic(),'C_free_bytes':shutil.disk_usage('/mnt/c').free,
                    'loadavg':os.getloadavg(),'servers':servers,'nspawn_count':len(launches),'container_processes':inside,'jobs':{}}
                for label,entry in jobs.items():
                    row=current[entry['id']];latest=read_json(job_dir(row['id'])/'latest.json',{})
                    native=read_json(job_dir(row['id'])/'child.json')
                    p=None
                    if native:
                        if row['server_id']=='compute-3':
                            p=next((v for v in inside if v['container_pid']==native['pid'] and '/mono_export ' in v['argv']),None)
                        else: p=process(native['pid'])
                    sample['jobs'][label]={k:row[k] for k in ('status','server_id','worker_pid','processed_frames','started_at','finished_at')}
                    sample['jobs'][label].update(native=p,frame_id=latest.get('frame_id'),tracking_state=latest.get('tracking_state'))
                out.write(json.dumps(sample)+'\n');atomic_json(root/'observation.json',sample)
                if (root/'stop').exists(): break
                time.sleep(.5)
    except BaseException as e:
        atomic_json(root/'observer-error.json',{'at':utc(),'error':f'{type(e).__name__}: {e}'})
        if (root/'session.json').exists():
            with client(root) as c:
                for entry in read_json(root/'jobs.json',{}).values(): c.post(f'/api/jobs/{entry["id"]}/cancel')
        raise


def finish(root):
    jobs=read_json(root/'jobs.json');before=read_json(root/'before.json');after=rows()
    validations={}
    with client(root) as c:
        for k,entry in jobs.items():
            validations[k]=validate(c,entry['id'],entry['frames'])
            assert not (job_dir(entry['id'])/'sequence/rgb').exists()
            assert read_json(job_dir(entry['id'])/'termination.json')['group_members_after']==[]
            snap=c.get(f'/api/jobs/{entry["id"]}/snapshot').json();assert c.get(snap['frame_url']).status_code==200
    checks={'all_previous_rows_owners_preserved':all(after[j]==r for j,r in before['jobs'].items()),
        'all_previous_files_preserved':all(signature(job_dir(j))==v for j,v in before['files'].items()),
        'exactly_four_new_jobs':set(after)-set(before['jobs'])=={v['id'] for v in jobs.values()} and len(jobs)==4,
        'core_preserved':all(hashlib.sha256((CV_ROOT/n).read_bytes()).hexdigest()==v for n,v in before['core'].items()),
        'R5_preserved':all(hashlib.sha256((DATA/'reports'/n).read_bytes()).hexdigest()==v for n,v in before['r5'].items()),
        'R6_preserved':all(hashlib.sha256((DATA/'reports/queue-356124ce0fb14080873fd255b3fbec2f'/n).read_bytes()).hexdigest()==v for n,v in before['r6'].items()),
        'limits_preserved':LIMITS['stored_jobs']==100 and LIMITS['queued_jobs']==10}
    with engine.connect() as c: users=list(c.execute(text('SELECT row_to_json(users) FROM users ORDER BY id')).scalars())
    checks['users_preserved']=hashlib.sha256(json.dumps(users,sort_keys=True).encode()).hexdigest()==before['users_sha256']
    checks['container_has_validated_result']=any(v['state']['server_id']=='compute-3' for v in validations.values())
    with (DATA/'logs/nginx-access.log').open() as f:
        f.seek(before['nginx_log_offset']);routes=[line.strip() for line in f if ' /execute ' in line and any(v['id'] in line for v in jobs.values())]
    checks['new_server_via_nginx']=any('upstream=127.0.0.1:8103' in line and 'server=compute-3' in line and ' 202 ' in line for line in routes)
    series=[json.loads(line) for line in (root/'series.jsonl').read_text().splitlines()]
    checks['real_native_on_each_server']=all(any(j['server_id']==name and j['native']
        for s in series for j in s['jobs'].values()) for name in ('compute-1','compute-2','compute-3'))
    checks['fourth_waited_while_three_occupied']=any(sum(j['status']=='running' for j in s['jobs'].values())==3
        and any(j['status']=='queued' for j in s['jobs'].values()) for s in series)
    assert all(checks.values()),checks
    with (DATA/'logs/autoscaler.log').open() as f:
        f.seek(before['autoscaler_log_offset']);events=f.read()
    atomic_json(root/'nginx-routes.json',routes);(root/'events.log').write_text(events)
    atomic_json(root/'validation.json',{'passed':True,'checks':checks,'jobs':validations,'C_free_bytes':shutil.disk_usage('/mnt/c').free,'previous_jobs':len(before['jobs']),'saved_jobs':len(after)})
    print(json.dumps({'passed':True,'saved_jobs':len(after),'results':{k:v['result'] for k,v in validations.items()}}))


def run(root):
    """Exactly four short videos in one load scenario, all through HTTPS/Nginx."""
    if (root/'run.json').exists(): raise ValueError('Run evidence already exists; no automatic repeat')
    ensure_space()
    if shutil.disk_usage('/mnt/c').free<10*2**30: raise ValueError('Initial C: below 10 GiB')
    evidence={'started_at':utc(),'passed':False,'jobs':{},'requests':[],'observations':[],'snapshots':{}}
    c=httpx.Client(base_url=tls.canonical_origin(),verify=ssl.create_default_context(cafile=tls.paths()['ca_cert']),timeout=20,trust_env=False)
    def save(): atomic_json(root/'run.json',evidence)
    def request(method,path,expected=200,**kwargs):
        started=time.monotonic();r=c.request(method,path,**kwargs)
        evidence['requests'].append({'at':utc(),'method':method,'path':path,'http':r.status_code,'seconds':time.monotonic()-started})
        assert r.status_code==expected,(path,r.status_code)
        return r
    def submit(label,seconds):
        video=CV_ROOT/f'datasets/freiburg1_xyz-rgb-30fps-short-{seconds}s.mp4'
        with video.open('rb') as media:
            value=request('POST','/api/jobs',202,files={'video':(video.name,media,'video/mp4')},data={'parameters':video.with_suffix('.parameters.json').read_text()}).json()
        evidence['jobs'][label]={'id':value['id'],'frames':seconds*30,'created_at':value['created_at']}
        atomic_json(root/'jobs.json',evidence['jobs']);save()
    def sample():
        fault=read_json(root/'observer-error.json')
        if fault: raise RuntimeError(fault['error'])
        ensure_space()
        value={'at':utc(),'monotonic':time.monotonic(),'servers':registry(),'jobs':{}}
        for label,entry in evidence['jobs'].items():
            state=request('GET',f'/api/jobs/{entry["id"]}').json()
            assert state['status'] not in ('failed','cancelled','timed_out'),(label,state['status'],state['error'])
            value['jobs'][label]=state
            previous=evidence['snapshots'].get(label,[])
            if state['status']=='running' and state['processed_frames']>0 and (not previous or state['processed_frames']-previous[-1]['frame_id']>=30):
                snap=request('GET',f'/api/jobs/{entry["id"]}/snapshot').json()
                if snap.get('frame_id') is not None:
                    previous.append({'at':utc(),'frame_id':snap['frame_id'],'tracking_state':snap.get('tracking_state'),
                        'map_id':snap.get('map_id'),'segment_id':snap.get('segment_id'),'points':len(snap.get('points',[]))})
                    evidence['snapshots'][label]=previous
        evidence['observations'].append(value);save()
        if value['servers']['compute-3']['state']=='failed': raise RuntimeError(value['servers']['compute-3']['error'])
        return value,read_json(root/'observation.json',{})
    try:
        initial=registry()
        assert {k for k,r in initial.items() if r['state']=='ready'}=={'compute-1','compute-2'}
        assert not runtime.owned_launches()
        evidence['initial_servers']=initial
        bootstrap=request('GET','/api/auth/session').json();c.headers.update({'Origin':tls.canonical_origin(),'X-CSRF-Token':bootstrap['csrf_token']})
        auth=request('POST','/api/auth/login',json=read_json(root/'credentials.json')).json()
        c.headers['X-CSRF-Token']=auth['csrf_token']
        private_json(root/'session.json',{'cookie':c.cookies.get('mtw_session'),'csrf':auth['csrf_token']})
        submit('A',10);submit('B',12)
        deadline=time.monotonic()+100
        while True:
            state,observed=sample()
            if all(observed.get('jobs',{}).get(k,{}).get('native') and observed['jobs'][k]['processed_frames']>0 for k in ('A','B')): break
            assert all(j['status']!='succeeded' for j in state['jobs'].values()),'Baseline finished before load trigger'
            assert time.monotonic()<deadline,'Baseline native startup timeout'
            time.sleep(.3)
        evidence['before_load_trigger']=observed
        submit('C',10)
        deadline=time.monotonic()+100
        while True:
            state,observed=sample()
            if state['jobs']['C']['status']=='running':
                assert state['jobs']['C']['server_id']=='compute-3','Third job did not use newly launched container'
                # R7 requires an automatic ready server taking a real job;
                # baseline clips may finish while the new native loads its vocabulary.
                break
            assert time.monotonic()<deadline,'Automatic container startup/dispatch timeout'
            time.sleep(.3)
        evidence['third_server_before_fourth']=observed
        submit('D',10)
        deadline=time.monotonic()+350
        while True:
            state,observed=sample()
            if all(j['status']=='succeeded' for j in state['jobs'].values()): break
            assert time.monotonic()<deadline,'Acceptance time budget exceeded'
            time.sleep(.5)
        finish(root)
        evidence['passed']=True
    except BaseException as error:
        evidence['error']=f'{type(error).__name__}: {error}'
        for entry in evidence['jobs'].values():
            try:request('POST',f'/api/jobs/{entry["id"]}/cancel',202)
            except Exception:pass
        raise
    finally:
        evidence['finished_at']=utc();save()
        try:request('POST','/api/auth/logout',204)
        except Exception:pass
        c.close()


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=('prepare','session','observe','finish','run'));p.add_argument('--root',type=Path);p.add_argument('--expected-jobs',type=int,default=54);a=p.parse_args()
    if a.action=='prepare': return prepare(a.expected_jobs)
    root=a.root.resolve();assert root.parent==(DATA/'reports').resolve() and root.name.startswith('autoscale-') and len(root.name)==42
    if a.action=='session': return private_json(root/'session.json',json.load(sys.stdin))
    if a.action=='observe': return observe(root)
    if a.action=='run': return run(root)
    finish(root)


if __name__=='__main__': main()
