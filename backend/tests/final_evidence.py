"""Final acceptance evidence: preserve saved jobs, validate one allowed browser job."""
import argparse
import ast
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import shutil
import ssl
import subprocess
from uuid import uuid4
import httpx
from sqlalchemy import text
from mtw.config import DATA,CV_ROOT,TERMINAL,job_dir,LIMITS
from mtw.capacity import ensure_space
from mtw.db import engine
from mtw.files import atomic_json,read_json
from mtw import tls
from parallel_e2e import signature
from check_parallel_results import validate


def utc():return datetime.now(timezone.utc).isoformat()


def rows():
    with engine.connect() as c:return {r['id']:r for r in c.execute(text('SELECT row_to_json(jobs) FROM jobs')).scalars()}


def user_hash():
    with engine.connect() as c:users=list(c.execute(text('SELECT row_to_json(users) FROM users ORDER BY id')).scalars())
    return hashlib.sha256(json.dumps(users,sort_keys=True).encode()).hexdigest()


def git_source(commit,path):return subprocess.check_output(['git','show',commit+':'+path]).decode('utf-8')


def timeout_code_proof():
    path='backend/mtw/compute.py'
    def pieces(source):
        fn=next(n for n in ast.parse(source).body if isinstance(n,ast.AsyncFunctionDef) and n.name=='run_job')
        branches={}
        for node in ast.walk(fn):
            if isinstance(node,ast.If) and ast.unparse(node.test) in ('cancel','time.monotonic() - started >= budget'):
                branches[ast.unparse(node.test)]=ast.dump(node,include_attributes=False)
        outer=next(n for n in fn.body if isinstance(n,ast.Try))
        prefix=[]
        for n in outer.finalbody:
            if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='elapsed' for t in n.targets):break
            prefix.append(ast.dump(n,include_attributes=False))
        branches['terminate_wait_reap_cleanup_prefix']=prefix
        return branches
    old,new=pieces(git_source('dafba89',path)),pieces(git_source('HEAD',path))
    values={key:old[key]==new[key] for key in old}
    for path in ('backend/mtw/processes.py','backend/mtw/frame_work.py'):
        values[path]=git_source('dafba89',path)==git_source('HEAD',path)
    assert all(values.values()),values
    return {'baseline':'dafba89','current':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        'unchanged':values,'note':'Full run_job changed successful video_info metadata in R6; timeout branches and resource cleanup are unchanged.'}


def prepare():
    ensure_space();assert shutil.disk_usage('/mnt/c').free>=10*2**30
    saved=rows();assert len(saved)==72 and all(j['status'] in TERMINAL for j in saved.values())
    root=DATA/'reports'/('final-'+uuid4().hex);root.mkdir(mode=0o700)
    previous=DATA/'reports/admin-14da9ecf049443b5b1dbe6f97504205d'
    source=read_json(previous/'before.json');proofs=source['proofs'].copy()
    for name in ('real.json','browser.json','container-review-browser.json','container-review-probe.json','metric-validation.json','preservation.json','production-access.json','test-summary.json'):
        path=previous/name;proofs[str(path)]=hashlib.sha256(path.read_bytes()).hexdigest()
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==v for p,v in proofs.items())
    atomic_json(root/'before.json',{'at':utc(),'rows':saved,'files':{j:signature(job_dir(j)) for j in saved},
        'users_hash':user_hash(),'core':source['core'],'proofs':proofs,'C_free_bytes':shutil.disk_usage('/mnt/c').free,
        'native_free_bytes':shutil.disk_usage(DATA).free})
    atomic_json(root/'timeout-code.json',timeout_code_proof())
    print(json.dumps({'root':str(root),'saved_jobs':len(saved),'C_free_bytes':shutil.disk_usage('/mnt/c').free}))


def authenticated():
    c=httpx.Client(base_url=tls.canonical_origin(),verify=ssl.create_default_context(cafile=tls.paths()['ca_cert']),timeout=20,trust_env=False)
    c.headers.update({'Origin':tls.canonical_origin(),'X-CSRF-Token':c.get('/api/auth/session').json()['csrf_token']})
    r=c.post('/api/auth/login',json=read_json(DATA/'reports/queue-356124ce0fb14080873fd255b3fbec2f/credentials.json'));r.raise_for_status()
    c.headers['X-CSRF-Token']=r.json()['csrf_token'];return c


def verify(root,after_restart=False):
    before=read_json(root/'before.json');after=rows();job_id=read_json(root/'job.json')['id']
    checks={'all_72_rows_owners':all(after[j]==r for j,r in before['rows'].items()),
        'all_72_file_signatures':all(signature(job_dir(j))==v for j,v in before['files'].items()),
        'users_roles_password_hashes':user_hash()==before['users_hash'],
        'core':all(hashlib.sha256((CV_ROOT/p).read_bytes()).hexdigest()==h for p,h in before['core'].items()),
        'R5_R6_R7_R8':all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in before['proofs'].items()),
        'one_new_job':set(after)-set(before['rows'])=={job_id},'limits':LIMITS['stored_jobs']==100 and LIMITS['queued_jobs']==10}
    c=authenticated()
    try:
        result=validate(c,job_id,300)
        s=c.get(f'/api/jobs/{job_id}/snapshot').json();checks['preview_after_cleanup']=c.get(s['frame_url']).status_code==200
        c.post('/api/auth/logout')
    finally:c.close()
    checks['work_frames_removed']=not (job_dir(job_id)/'sequence/rgb').exists()
    checks['group_empty']=read_json(job_dir(job_id)/'termination.json')['group_members_after']==[]
    if after_restart:
        checkpoint=read_json(root/'restart-before.json')
        checks['restart_all_73_rows']=checkpoint['rows']==after
        checks['restart_all_73_files']=all(signature(job_dir(j))==v for j,v in checkpoint['files'].items())
    assert all(checks.values()),checks
    proof={'at':utc(),'checks':checks,'jobs':len(after),'result':result,'C_free_bytes':shutil.disk_usage('/mnt/c').free}
    atomic_json(root/('after-restart.json' if after_restart else 'validated.json'),proof)
    if not after_restart:atomic_json(root/'restart-before.json',{'rows':after,'files':{j:signature(job_dir(j)) for j in after}})
    print(json.dumps({'checks':checks,'jobs':len(after),'result':result['result']}))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('prepare','validate','after-restart'));p.add_argument('--root',type=Path);a=p.parse_args()
    if a.action=='prepare':prepare()
    else:
        root=a.root.resolve();assert root.parent==(DATA/'reports').resolve() and root.name.startswith('final-') and len(root.name)==38
        verify(root,a.action=='after-restart')
