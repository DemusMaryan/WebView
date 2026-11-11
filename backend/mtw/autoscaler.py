"""Root-only project controller: two native baselines, at most one nspawn third."""
import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from sqlalchemy import select
import httpx
from . import container_runtime as runtime
from .config import DATA,PORTS
from .db import ComputeServer,Job,Session,now
from .files import atomic_json,read_json
from .processes import identity
from .servers import BASE,SETTINGS,compute_route,reserve_launch,launch_failed
from .service_auth import service_headers

stopping=False


def event(name,**values):
    print(json.dumps({"at":now().isoformat(),"event":name,**values}),flush=True)


def health(name):
    try:
        response=httpx.get(f'http://127.0.0.1:{PORTS[name.replace("-","_")]}/health',headers=service_headers(),timeout=1,trust_env=False)
        value=response.json()
        return value if response.status_code==200 and value.get('server_id')==name else None
    except (httpx.HTTPError,ValueError): return None


def baseline_health():
    for name in BASE:
        value=health(name)
        with Session.begin() as s:
            row=s.get(ComputeServer,name)
            row.state='ready' if value else 'stopped'
            row.heartbeat_at=now() if value else None


def retire(info,namespace,process,status='failed',message='Container stopped'):
    with Session.begin() as s:
        row=s.get(ComputeServer,'compute-3',with_for_update=True)
        row.state,row.heartbeat_at='stopping',None
        for job in s.scalars(select(Job).where(Job.server_id=='compute-3',Job.status=='running')):
            job.cancel_requested=True
    from .control import write_nginx
    try:
        write_nginx(exclude=('compute-3',))
    except Exception as error:
        # A broken proxy/TLS configuration must not strand an owned namespace.
        event('nginx_remove_failed',error=type(error).__name__+': '+str(error))
    runtime.stop(info,namespace,process)
    from .frame_work import cleanup_decoded_frames
    with Session.begin() as s:
        for job in s.scalars(select(Job).where(Job.server_id=='compute-3',Job.status=='running')):
            cleanup_decoded_frames(DATA/'jobs'/str(job.id))  # Namespace verified empty above.
            job.status=job.stage='cancelled' if status=='stopped' else 'failed'
            job.error=message; job.finished_at=job.updated_at=now()
        row=s.get(ComputeServer,'compute-3',with_for_update=True)
        row.state,row.heartbeat_at,row.error=status,None,message
        row.process=None
        if status=='stopped': row.attempts,row.retry_after=0,None
    event('container_stopped',namespace=namespace,remaining=runtime.namespace_members(namespace))


def run():
    global stopping
    runtime.require_root()
    lock_path=runtime.ROOT/'controller.lock'
    runtime.ROOT.mkdir(parents=True,exist_ok=True)
    with lock_path.open('a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError('Project autoscaler already owns its lifecycle lock') from None
        atomic_json(DATA/'pids/autoscaler.json',identity(os.getpid())|{'marker':'mtw.autoscaler run'})
        os.chown(DATA/'pids/autoscaler.json',1000,1000)
        for sig in (signal.SIGTERM,signal.SIGINT): signal.signal(sig,lambda *_:request_stop())
        process,info,namespace=None,None,None
        failures=0
        with Session() as s:
            record=s.get(ComputeServer,'compute-3')
            namespace=(record.process or {}).get('pid_namespace')
            old=(record.process or {}).get('supervisor')
        launches=runtime.owned_launches()
        if len(launches)>1: raise RuntimeError('More than one project nspawn found; refuse automatic replay')
        if launches:
            info=launches[0]
            value=health('compute-3')
            if value: namespace=value['pid_namespace']
            if value and namespace != os.readlink('/proc/self/ns/pid'):
                from .control import write_nginx
                write_nginx(add_server='compute-3')
                with Session.begin() as s:
                    record=s.get(ComputeServer,'compute-3'); record.state='ready'; record.heartbeat_at=now()
                event('adopted_existing_container',pid=info['pid'])
            else:
                retire(info,namespace,None,message='Interrupted startup recovered without replay')
                info,namespace=None,None
        elif namespace or old:
            retire(old,namespace,None,message='Previous container exited; namespace reconciled')
        else:
            with Session.begin() as s:
                record=s.get(ComputeServer,'compute-3')
                if record.state in {'starting','ready','stopping'}:
                    launch_failed(str(record.launch_token),'Interrupted launch without surviving container')
        event('controller_ready',base=2,maximum=3)
        try:
            while not stopping:
                baseline_health()
                if info:
                    value=health('compute-3')
                    if process: process.poll()
                    alive=runtime.supervisor_alive(info)
                    if value and alive:
                        failures=0
                        with Session.begin() as s:
                            record=s.get(ComputeServer,'compute-3'); record.heartbeat_at=now()
                    else:
                        failures+=1
                    if not alive or failures>=3:
                        with Session() as s: token=str(s.get(ComputeServer,'compute-3').launch_token)
                        retire(info,namespace,process,message='Container health/process lost')
                        launch_failed(token,'Container health/process lost')
                        process,info,namespace=None,None,None
                if info is None:
                    token=reserve_launch()
                    if token:
                        event('launch_reserved',server_id='compute-3',token=token)
                        try:
                            runtime.prepare()
                            process=runtime.launch()
                            deadline=time.monotonic()+SETTINGS['startup_seconds']
                            info=identity(process.pid)
                            while info is None and process.poll() is None and time.monotonic()<deadline:
                                time.sleep(.01); info=identity(process.pid)
                            if info is None: raise RuntimeError('nspawn exited before PID ownership was recorded')
                            with Session.begin() as s:
                                record=s.get(ComputeServer,'compute-3'); record.process={'supervisor':{k:info[k] for k in ('pid','start_ticks')}}
                            value=None
                            while time.monotonic()<deadline and not stopping:
                                namespace=namespace or runtime.namespace_of_supervisor(info)
                                if process.poll() is not None: raise RuntimeError(f'nspawn exit code {process.returncode}; inspect nspawn-compute-3.log')
                                value=health('compute-3')
                                if value: break
                                time.sleep(.1)
                            if not value: raise TimeoutError('Container did not become healthy within startup limit')
                            namespace=value['pid_namespace']
                            if runtime.namespace_of_supervisor(info)!=namespace:
                                raise RuntimeError('Health endpoint is not a child of the owned nspawn supervisor')
                            if namespace == os.readlink('/proc/self/ns/pid') or value['mount_namespace']==os.readlink('/proc/self/ns/mnt'):
                                raise RuntimeError('Endpoint is not in isolated PID/mount namespaces')
                            if not any(p['container_pid']==value['pid'] and '-m mtw.service compute-3' in p['argv'] for p in runtime.namespace_members(namespace)):
                                raise RuntimeError('Cannot verify container process from host namespace')
                            protection=value.get('isolation',{})
                            if (not all(protection.get(k) for k in ('root_read_only','repository_read_only','core_read_only','jobs_writable'))
                                    or protection.get('host_only_marker_visible') or protection.get('uid')!=1000 or protection.get('no_new_privileges')!='1'):
                                raise RuntimeError('Container filesystem/user protections are not verified')
                            with Session.begin() as s:
                                record=s.get(ComputeServer,'compute-3'); record.process={'supervisor':{k:info[k] for k in ('pid','start_ticks')},**value}
                            from .control import write_nginx
                            write_nginx(add_server='compute-3')
                            proxy_deadline=time.monotonic()+5
                            proxy_ready=False
                            while time.monotonic()<proxy_deadline:
                                try:
                                    probe=httpx.get(f'http://127.0.0.1:{PORTS["balancer"]}{compute_route("compute-3", "health")}',headers=service_headers(),timeout=1,trust_env=False)
                                    if probe.status_code==200 and probe.json().get('server_id')=='compute-3': proxy_ready=True; break
                                except (httpx.HTTPError,ValueError): pass
                                time.sleep(.1)
                            if not proxy_ready: raise RuntimeError('Nginx did not publish healthy compute-3')
                            with Session.begin() as s:
                                record=s.get(ComputeServer,'compute-3'); record.state='ready'; record.ready_at=record.heartbeat_at=now()
                            event('container_ready',server_id='compute-3',supervisor_pid=info['pid'],isolation=value)
                        except Exception as error:
                            if info: retire(info,namespace,process,message=type(error).__name__+': '+str(error))
                            launch_failed(token,type(error).__name__+': '+str(error))
                            event('launch_failed',error=type(error).__name__+': '+str(error))
                            process,info,namespace=None,None,None
                deadline=time.monotonic()+SETTINGS['health_interval_seconds']
                while not stopping and time.monotonic()<deadline: time.sleep(.1)
        finally:
            if info: retire(info,namespace,process,status='stopped',message='Operator stopped project controller')
            event('controller_stopped')


def request_stop():
    global stopping
    stopping=True


def main():
    runtime.require_root()
    parser=argparse.ArgumentParser(); parser.add_argument('action',choices=['setup','start','run','stop','reset-failure']); args=parser.parse_args()
    if args.action=='setup': print(json.dumps(runtime.prepare())); return
    if args.action=='run': return run()
    if args.action=='reset-failure':
        if runtime.owned_launches(): raise RuntimeError('Stop the project container before resetting failure history')
        with Session.begin() as s:
            row=s.get(ComputeServer,'compute-3',with_for_update=True)
            if row.state not in ('failed','stopped'): raise RuntimeError('Only stopped/failed launches may be reset explicitly')
            row.attempts,row.retry_after,row.error,row.process,row.state=0,None,None,None,'stopped'
        return
    record=read_json(DATA/'pids/autoscaler.json',{})
    item=identity(record.get('pid',0))
    owned=item and item.get('start_ticks')==record.get('start_ticks') and 'mtw.autoscaler run' in item['argv']
    if args.action=='start':
        if owned: return
        with (DATA/'logs/autoscaler.log').open('ab') as log:
            os.chown(log.name,1000,1000)
            child=subprocess.Popen([sys.executable,'-m','mtw.autoscaler','run'],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            if child.poll() is not None: raise RuntimeError('Autoscaler exited; inspect logs/autoscaler.log')
            saved=read_json(DATA/'pids/autoscaler.json',{})
            if saved.get('pid')==child.pid: return
            time.sleep(.05)
        raise RuntimeError('Autoscaler PID record was not published')
    if owned:
        os.kill(item['pid'],signal.SIGTERM)
        deadline=time.monotonic()+40
        while identity(item['pid']) and time.monotonic()<deadline: time.sleep(.1)
        if identity(item['pid']): raise RuntimeError('Autoscaler/container stop exceeded limit; inspect without replay')
    else:
        launches=runtime.owned_launches()
        if len(launches)>1: raise RuntimeError('Multiple project containers found; refuse ambiguous stop')
        if launches:
            value=health('compute-3')
            namespace=value['pid_namespace'] if value else runtime.namespace_of_supervisor(launches[0])
            retire(launches[0],namespace,None,status='stopped',message='Operator stopped orphaned project container')


if __name__=='__main__': main()
