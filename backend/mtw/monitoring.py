"""Project-only root collector; admin API reads a bounded cached file."""
import argparse
from datetime import timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import ssl
import subprocess
import sys
import time
import httpx
from sqlalchemy import create_engine,select,text
from sqlalchemy.orm import sessionmaker
from .config import DATA,PORTS,database_url
from .db import ComputeServer,Job,now
from .files import atomic_json,read_json
from .metrics import process_sample,system_sample,Rates,role_for
from .processes import identity
from .queue_eta import estimate_queue
from .servers import ready_servers
from .service_auth import service_headers
from . import container_runtime,tls

ROOT=DATA/'monitoring'
SNAPSHOT=ROOT/'latest.json'
INTERVAL=2.0
STALE_AFTER=6.0
MAX_PROCESSES=256
MAX_SCAN=8192
MAX_BYTES=256*1024
SERVICES=('api','dispatcher','nginx','autoscaler','monitor','compute-1','compute-2')


def boot_id():
    try: return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError: return None


def cached_snapshot(path=None,at=None,boot=None):
    path=path or SNAPSHOT
    unavailable={'status':'unavailable','stale':True,'age_seconds':None,'sampled_at':None,
        'services':[],'servers':[],'queue':[],'processes':[],'environment':None}
    try:
        if path.stat().st_size>MAX_BYTES: return unavailable
        value=read_json(path)
        if not isinstance(value,dict) or value.get('version')!=1: return unavailable
        age=(time.monotonic() if at is None else at)-value['sample_monotonic']
        same_boot=value['boot_id']==(boot_id() if boot is None else boot)
        stale=not same_boot or age<0 or age>STALE_AFTER
        return value|{'status':'stale' if stale else 'available','stale':stale,
            'age_seconds':round(age,2) if same_boot and age>=0 else None}
    except (OSError,ValueError,KeyError,TypeError): return unavailable


def verified_record(name,proc):
    try:
        record=read_json(DATA/'pids'/f'{name}.json',{})
        item=proc.get(record.get('pid'))
        if item and str(item['start_ticks'])==str(record.get('start_ticks')) and record['marker'].encode() in b' '.join(item['_argv']):
            return item['host_pid']
    except (OSError,ValueError,KeyError): pass
    return None


class Collector:
    def __init__(self,sessions,client=None):
        self.sessions=sessions;self.client=client or httpx.Client(timeout=.4,trust_env=False)
        self.rates=Rates()
        self.boot=boot_id()
        self.host_namespace=os.readlink('/proc/self/ns/pid')

    def health(self,name):
        try:
            if name=='nginx':
                r=self.client.get(tls.canonical_origin()+'/health')
            else:
                port=PORTS[name.replace('-','_')]
                r=self.client.get(f'http://127.0.0.1:{port}/health',headers=service_headers() if name.startswith('compute-') else {})
            return 'healthy' if r.status_code==200 else 'unhealthy'
        except (httpx.HTTPError,OSError,RuntimeError): return 'unavailable'

    def collect(self):
        at=time.monotonic();timestamp=now().isoformat()
        proc={}
        paths=[p for p in Path('/proc').iterdir() if p.name.isdigit()]
        for path in paths[:MAX_SCAN]:
            p=process_sample(int(path.name))
            if p: proc[p['host_pid']]=p
        roots={};services=[]
        for name in SERVICES:
            pid=verified_record(name,proc)
            if pid: roots[pid]=(name,'compute' if name.startswith('compute-') else name,None)
            services.append({'name':name,'pid':pid,'running':pid is not None,
                'health':self.health(name) if name in {'api','nginx','compute-1','compute-2'} else ('process_present' if pid else 'missing')})
        pg=None
        try:
            pid=int((DATA/'pgdata/postmaster.pid').read_text().splitlines()[0]);p=proc.get(pid)
            if p and str(DATA/'pgdata').encode() in b' '.join(p['_argv']): pg=pid;roots[pid]=('postgres','postgres',None)
        except (OSError,ValueError,IndexError): pass
        services.append({'name':'postgres','pid':pg,'running':pg is not None,'health':'unavailable'})
        queue=[];servers=[];jobs={};db_status='unavailable';ready=0
        try:
            with self.sessions() as s:
                s.execute(text('SET LOCAL statement_timeout=750'))
                active=list(s.scalars(select(Job).where(Job.status.in_(('dispatching','running'))).limit(11)))
                jobs={j.server_id:str(j.id) for j in active if j.server_id}
                ready_ids=ready_servers(s);ready=len(ready_ids)
                pending=list(s.scalars(select(Job).where(Job.status=='queued',Job.cancel_requested.is_(False)).order_by(Job.created_at,Job.id).limit(10)))
                for j in pending:
                    queue.append({'job_id':str(j.id),'state':j.status,**estimate_queue(s,j)})
                for row in s.scalars(select(ComputeServer).order_by(ComputeServer.server_id).limit(3)):
                    live=next((j for j in active if j.server_id==row.server_id),None)
                    servers.append({'server_id':row.server_id,'kind':row.kind,'registry_state':row.state,
                        'registry_fresh':row.server_id in ready_ids,'health':self.health(row.server_id),
                        'slot_limit':1,'slot_used':bool(live),'job_id':str(live.id) if live else None,
                        'job_state':live.status if live else None,'processed_frames':live.processed_frames if live else None,
                        'total_frames':live.total_frames if live else None})
                db_status='healthy'
        except Exception: pass  # Never expose database URLs, SQL parameters or exception text.
        services[-1]['health']=db_status
        for sup in container_runtime.owned_launches():
            if sup['pid'] in proc: roots[sup['pid']]=('compute-3','container_runtime',None)
        membership={}
        for pid in proc:
            parent=pid;seen=set()
            while parent in proc and parent not in seen:
                if parent in roots: membership[pid]=roots[parent];break
                seen.add(parent);parent=proc[parent]['ppid']
        records=[]
        for pid,(name,role,_) in membership.items():
            p=proc[pid];args=p.pop('_argv')
            server=name if name.startswith('compute-') else None
            words=[x.decode('utf-8','replace') for x in args if x]
            role=role_for(words,role,pid in roots,p['pid'])
            p.update(role=role,service=name,server_id=server,job_id=jobs.get(server),
                pid_scope='host' if p['pid_namespace']==self.host_namespace else 'container:'+str(server or 'unknown'))
            p.pop('ppid');records.append(p)
        records.sort(key=lambda p:p['host_pid'])
        env,ticks,uptime=system_sample()
        self.rates.apply(records,env,ticks,uptime,at)
        for p in records: p.pop('start_ticks')
        return {'version':1,'sampled_at':timestamp,'sample_monotonic':at,'boot_id':self.boot,
            'poll_interval_seconds':INTERVAL,'stale_after_seconds':STALE_AFTER,
            'environment':env,'services':services,'servers':servers,'queue':queue,'queue_status':db_status,
            'capacity':{'base':2,'maximum':3,'ready':ready if db_status=='healthy' else None,
                'active':len(active) if db_status=='healthy' else None},
            'processes':records[:MAX_PROCESSES],'process_count':len(records),
            'processes_truncated':len(records)>MAX_PROCESSES,'scan_truncated':len(paths)>MAX_SCAN,
            'collection_seconds':round(time.monotonic()-at,4),
            'units':{'memory':'bytes','process_cpu':'100%=one logical CPU; may exceed 100%',
                'environment_cpu':'0..100% of all WSL logical CPUs','RSS':'per-process resident; shared pages included; do not sum'}}


def publish(value):
    if len(json.dumps(value).encode())>MAX_BYTES: raise ValueError('Monitoring response bound exceeded')
    atomic_json(SNAPSHOT,value)
    os.chmod(SNAPSHOT,0o600);os.chown(SNAPSHOT,DATA.stat().st_uid,DATA.stat().st_gid)


def run():
    container_runtime.require_root()
    os.umask(0o077)
    ROOT.mkdir(mode=0o700,exist_ok=True);os.chown(ROOT,DATA.stat().st_uid,DATA.stat().st_gid)
    with (ROOT/'collector.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        atomic_json(DATA/'pids/monitor.json',identity(os.getpid())|{'marker':'mtw.monitoring run'})
        os.chown(DATA/'pids/monitor.json',DATA.stat().st_uid,DATA.stat().st_gid)
        stopping=False
        def stop(*_):
            nonlocal stopping
            stopping=True
        for sig in (signal.SIGTERM,signal.SIGINT): signal.signal(sig,stop)
        engine=create_engine(database_url(),pool_size=1,max_overflow=0,pool_timeout=1,
            connect_args={'connect_timeout':1,'options':'-c statement_timeout=750'})
        with httpx.Client(timeout=.4,trust_env=False,verify=ssl.create_default_context(cafile=tls.paths()['ca_cert'])) as client:
            collector=Collector(sessionmaker(engine),client)
            try:
                while not stopping:
                    started=time.monotonic()
                    try: publish(collector.collect())
                    except Exception as error: print('monitor_sample_failed '+type(error).__name__,flush=True)
                    while not stopping and time.monotonic()-started<INTERVAL: time.sleep(.1)
            finally: engine.dispose()


def main():
    container_runtime.require_root()
    p=argparse.ArgumentParser();p.add_argument('action',choices=('start','stop','run'));a=p.parse_args()
    if a.action=='run': return run()
    record=read_json(DATA/'pids/monitor.json',{});item=identity(record.get('pid',0))
    owned=item and item['start_ticks']==record.get('start_ticks') and 'mtw.monitoring run' in item['argv']
    if a.action=='start':
        if owned: return
        with (DATA/'logs/monitor.log').open('ab') as log:
            child=subprocess.Popen([sys.executable,'-m','mtw.monitoring','run'],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            if child.poll() is not None: raise RuntimeError('Project monitor exited; inspect monitor.log')
            if read_json(DATA/'pids/monitor.json',{}).get('pid')==child.pid: return
            time.sleep(.05)
        raise RuntimeError('Project monitor PID was not published')
    elif owned:
        os.kill(item['pid'],signal.SIGTERM);deadline=time.monotonic()+8
        while identity(item['pid']) and time.monotonic()<deadline: time.sleep(.1)
        if identity(item['pid']): raise RuntimeError('Project monitor did not stop within limit')


if __name__=='__main__': main()
