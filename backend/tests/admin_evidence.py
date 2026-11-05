"""Explicit one-video R8 evidence; never grants an administrator in public DB."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import shutil
import ssl
import time
from uuid import uuid4
import httpx
from sqlalchemy import text
from mtw.config import DATA,CV_ROOT,job_dir,TERMINAL,LIMITS
from mtw.db import engine
from mtw.capacity import ensure_space
from mtw.files import atomic_json,read_json
from mtw import tls
from mtw.metrics import process_sample
from mtw.monitoring import cached_snapshot
from parallel_e2e import signature
from check_parallel_results import validate


def utc():return datetime.now(timezone.utc).isoformat()


def rows():
    with engine.connect() as c:return {r['id']:r for r in c.execute(text('SELECT row_to_json(jobs) FROM jobs')).scalars()}


def users_hash():
    with engine.connect() as c:users=list(c.execute(text('SELECT row_to_json(users) FROM users ORDER BY id')).scalars())
    for u in users:u.pop('is_admin',None)
    return hashlib.sha256(json.dumps(users,sort_keys=True).encode()).hexdigest()


def prepare():
    ensure_space();assert shutil.disk_usage('/mnt/c').free>=10*2**30
    jobs=rows();assert len(jobs)==71 and all(j['status'] in TERMINAL for j in jobs.values())
    root=DATA/'reports'/('admin-'+uuid4().hex);root.mkdir(mode=0o700)
    r7=DATA/'reports/autoscale-0debce34bd9a485bae08a848e392cccf'
    source=read_json(r7/'before.json')
    proofs={str(DATA/'reports'/n):h for n,h in source['r5'].items()}
    proofs.update({str(DATA/'reports/queue-356124ce0fb14080873fd255b3fbec2f'/n):h for n,h in source['r6'].items()})
    for name in ('run.json','validation.json','facts.json','series.jsonl','after-stop.json'):proofs[str(r7/name)]=hashlib.sha256((r7/name).read_bytes()).hexdigest()
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in proofs.items())
    atomic_json(root/'before.json',{'at':utc(),'rows':jobs,'files':{j:signature(job_dir(j)) for j in jobs},
        'users_hash':users_hash(),'core':source['core'],'proofs':proofs,'C_free_bytes':shutil.disk_usage('/mnt/c').free})
    print(json.dumps({'root':str(root),'jobs':len(jobs),'C_free_bytes':shutil.disk_usage('/mnt/c').free}))


def real(root):
    if (root/'real.json').exists():raise ValueError('Existing run evidence; no automatic repeat')
    ensure_space();assert shutil.disk_usage('/mnt/c').free>=10*2**30
    evidence={'started_at':utc(),'passed':False,'requests':[],'monitor_samples':[]}
    video=CV_ROOT/'datasets/freiburg1_xyz-rgb-30fps-short-10s.mp4'
    credentials=read_json(DATA/'reports/queue-356124ce0fb14080873fd255b3fbec2f/credentials.json')
    with httpx.Client(base_url=tls.canonical_origin(),verify=ssl.create_default_context(cafile=tls.paths()['ca_cert']),timeout=20,trust_env=False) as c:
        def req(method,path,**kwargs):
            r=c.request(method,path,**kwargs);evidence['requests'].append({'at':utc(),'method':method,'path':path,'status':r.status_code});r.raise_for_status();return r
        try:
            r=req('GET','/api/auth/session');c.headers.update({'Origin':tls.canonical_origin(),'X-CSRF-Token':r.json()['csrf_token']})
            r=req('POST','/api/auth/login',json=credentials);c.headers['X-CSRF-Token']=r.json()['csrf_token']
            assert not r.json()['user']['is_admin']
            with video.open('rb') as f:
                job=req('POST','/api/jobs',files={'video':(video.name,f,'video/mp4')},data={'parameters':video.with_suffix('.parameters.json').read_text()}).json()
            evidence['job_id']=job['id'];atomic_json(root/'real.json',evidence)
            deadline=time.monotonic()+160
            while time.monotonic()<deadline:
                ensure_space()
                state=req('GET',f'/api/jobs/{job["id"]}').json()
                metric=cached_snapshot()
                processes=[p for p in metric.get('processes',[]) if p['job_id']==job['id']]
                native=next((p for p in processes if p['role']=='native'),None)
                raw=process_sample(native['host_pid']) if native else None
                if raw:raw.pop('_argv')
                evidence['monitor_samples'].append({'at':utc(),'state':state,'sampled_at':metric['sampled_at'],
                    'sample_monotonic':metric.get('sample_monotonic'),'stale':metric['stale'],'processes':processes,'kernel_native':raw})
                atomic_json(root/'real.json',evidence)
                if state['status'] in TERMINAL:
                    assert state['status']=='succeeded',(state['status'],state['error']);break
                time.sleep(.5)
            else:raise TimeoutError('R8 one-video budget exceeded')
            evidence['validation']=validate(c,job['id'],300)
            assert any(any(p['role']=='worker' for p in x['processes']) for x in evidence['monitor_samples'])
            assert any(any(p['role']=='native' and p['cpu_percent'] is not None and p['cpu_percent']>0 and p['rss_bytes']>0 for p in x['processes']) for x in evidence['monitor_samples'])
            assert not (job_dir(job['id'])/'sequence/rgb').exists()
            evidence['passed']=True
        except BaseException as e:
            evidence['error']=type(e).__name__+': '+str(e)
            if 'job_id' in evidence:
                try:req('POST',f'/api/jobs/{evidence["job_id"]}/cancel')
                except Exception:pass
            raise
        finally:
            evidence['finished_at']=utc();atomic_json(root/'real.json',evidence)
            try:req('POST','/api/auth/logout')
            except Exception:pass
    print(json.dumps({'passed':evidence['passed'],'job_id':evidence['job_id'],'validation':evidence.get('validation')}))


def verify(root):
    before=read_json(root/'before.json');after=rows();real=read_json(root/'real.json',{})
    with engine.connect() as c:admins=c.scalar(text('SELECT count(*) FROM users WHERE is_admin'))
    checks={'all_71_rows_owners':all(after[j]==r for j,r in before['rows'].items()),
        'all_71_file_signatures':all(signature(job_dir(j))==sig for j,sig in before['files'].items()),
        'users_unchanged_except_default_role':users_hash()==before['users_hash'],'production_admins_zero':admins==0,
        'core':all(hashlib.sha256((CV_ROOT/p).read_bytes()).hexdigest()==h for p,h in before['core'].items()),
        'R5_R6_R7':all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in before['proofs'].items()),
        'exactly_one_new_job':set(after)-set(before['rows'])=={real.get('job_id')},
        'limits':LIMITS['stored_jobs']==100 and LIMITS['queued_jobs']==10}
    assert all(checks.values()),checks
    atomic_json(root/'preservation.json',{'at':utc(),'checks':checks,'jobs':len(after),'C_free_bytes':shutil.disk_usage('/mnt/c').free})
    print(json.dumps({'checks':checks,'jobs':len(after),'C_free_bytes':shutil.disk_usage('/mnt/c').free}))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('prepare','real','verify'));p.add_argument('--root',type=Path);a=p.parse_args()
    if a.action=='prepare':prepare()
    else:
        root=a.root.resolve();assert root.parent==(DATA/'reports').resolve() and root.name.startswith('admin-') and len(root.name)==38
        (real if a.action=='real' else verify)(root)
