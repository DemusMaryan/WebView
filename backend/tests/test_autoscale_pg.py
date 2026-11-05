"""Isolated PG concurrency/readiness/failure tests. No SLAM/container starts."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import timedelta
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,MagicMock
from uuid import uuid4
from sqlalchemy import create_engine,select,text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from mtw.config import DATA,database_url
from mtw.db import Base,ComputeServer,Job,now
from mtw import autoscaler,servers
from mtw.servers import reserve_launch,launch_failed,ready_servers,seed_test_baseline
from mtw.scheduler import reserve_next
from mtw.queue_eta import estimate_queue


@unittest.skipUnless(os.environ.get('MTW_POSTGRES_TESTS')=='1','Opt-in isolated PostgreSQL')
class AutoscalePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema='mtw_scale_'+uuid4().hex
        cls.admin=create_engine(database_url())
        with cls.admin.begin() as c: c.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
        cls.engine=create_engine(database_url(),pool_size=10,connect_args={'options':f'-csearch_path={cls.schema}'})
        Base.metadata.create_all(cls.engine); cls.sessions=sessionmaker(cls.engine,expire_on_commit=False)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        assert cls.schema.startswith('mtw_scale_') and len(cls.schema)==42
        with cls.admin.begin() as c: c.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
        cls.admin.dispose()

    def setUp(self):
        with self.sessions.begin() as s:
            s.execute(text('DELETE FROM jobs')); s.execute(text('DELETE FROM compute_servers'))
            seed_test_baseline(s)
            s.add(ComputeServer(server_id='compute-3',kind='nspawn',state='stopped'))
            for i in (1,2):
                s.add(Job(status='running',stage='processing',server_id=f'compute-{i}',parameters={},input_ref='no-worker',
                    started_at=now(),video_info={'frames':360,'width':640,'height':480}))
            job=Job(parameters={},input_ref='never-decoded')
            s.add(job);s.flush();self.queued=job.id
        self.enterContext(patch.dict(servers.SETTINGS,enabled=True,startup_seconds=.03,retry_seconds=30,max_start_attempts=2))

    def test_concurrent_cycles_reserve_exactly_one_start_and_preserve_queue(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:reserve_launch(self.sessions),range(8)))
        self.assertEqual(sum(r is not None for r in results),1)
        with self.sessions() as s:
            row=s.get(ComputeServer,'compute-3')
            self.assertEqual((row.state,row.attempts),('starting',1))
            self.assertEqual(s.get(Job,self.queued).status,'queued')
        self.assertIsNone(reserve_launch(self.sessions))

    def test_no_queue_or_one_busy_server_does_not_start(self):
        with self.sessions.begin() as s:
            s.get(Job,self.queued).cancel_requested=True
        self.assertIsNone(reserve_launch(self.sessions))
        with self.sessions.begin() as s:
            s.get(Job,self.queued).cancel_requested=False
            s.scalar(select(Job).where(Job.server_id=='compute-2')).status='succeeded'
        self.assertIsNone(reserve_launch(self.sessions))

    def test_failure_has_backoff_and_persistent_attempt_bound_without_losing_job(self):
        first=reserve_launch(self.sessions)
        launch_failed(first,'test startup failure',self.sessions)
        self.assertIsNone(reserve_launch(self.sessions))
        with self.sessions.begin() as s: s.get(ComputeServer,'compute-3').retry_after=now()-timedelta(seconds=1)
        second=reserve_launch(self.sessions);self.assertIsNotNone(second)
        launch_failed(second,'test second failure',self.sessions)
        with self.sessions.begin() as s: s.get(ComputeServer,'compute-3').retry_after=now()-timedelta(seconds=1)
        self.engine.dispose()
        self.assertIsNone(reserve_launch(self.sessions))
        with self.sessions() as s: self.assertEqual(s.get(Job,self.queued).status,'queued')

    def test_scheduler_counts_only_ready_slots_never_four(self):
        self.assertIsNone(reserve_next(self.sessions))
        reserve_launch(self.sessions)
        self.assertIsNone(reserve_next(self.sessions))
        with self.sessions.begin() as s:
            extra=s.get(ComputeServer,'compute-3');extra.state='ready';extra.heartbeat_at=now()
            s.add(Job(parameters={},input_ref='fourth-stays-queued'))
        self.assertIsNotNone(reserve_next(self.sessions))
        self.assertIsNone(reserve_next(self.sessions))
        self.assertIsNone(reserve_launch(self.sessions))
        with self.assertRaises(IntegrityError),self.sessions.begin() as s:
            s.add(ComputeServer(server_id='compute-4',kind='nspawn',state='starting'))

    def test_eta_uses_three_only_when_ready_and_ignores_stale_heartbeat(self):
        with self.sessions() as s:
            self.assertGreater(estimate_queue(s,s.get(Job,self.queued))['estimated_wait_seconds'],0)
        with self.sessions.begin() as s:
            extra=s.get(ComputeServer,'compute-3');extra.state='starting'
        with self.sessions() as s: self.assertEqual(len(ready_servers(s)),2)
        with self.sessions.begin() as s:
            extra=s.get(ComputeServer,'compute-3');extra.state='ready';extra.heartbeat_at=now()
        with self.sessions() as s:
            self.assertEqual(estimate_queue(s,s.get(Job,self.queued))['estimated_wait_seconds'],0)
        with self.sessions.begin() as s:
            s.get(ComputeServer,'compute-3').heartbeat_at=now()-timedelta(minutes=1)
        with self.sessions() as s: self.assertEqual(len(ready_servers(s)),2)

    def controller(self,failure=None):
        with tempfile.TemporaryDirectory(dir=DATA/'reports',prefix='scale-unit-') as directory:
            root=Path(directory);(root/'pids').mkdir()
            trace=[]; stopped=[False]
            info={'pid':999991,'start_ticks':'123','argv':'mock supervisor'}
            process=MagicMock(pid=999991);process.poll.return_value=None
            isolation={'root_read_only':True,'repository_read_only':True,'core_read_only':True,'jobs_writable':True,
                'host_only_marker_visible':False,'uid':1000,'no_new_privileges':'1'}
            value={'server_id':'compute-3','pid':4,'pid_namespace':'pid:[isolated-test]','mount_namespace':'mnt:[isolated-test]','isolation':isolation}
            def probe(name):
                trace.append('health:'+name)
                if name=='compute-3': return None if failure=='timeout' else value
                return {'server_id':name}
            def nginx(**kwargs):
                trace.append('nginx:add' if kwargs.get('add_server') else 'nginx:remove')
                if failure=='nginx-remove' and not kwargs.get('add_server'):
                    raise RuntimeError('fixture broken TLS during stop')
                with self.sessions() as s:
                    if kwargs.get('add_server'): self.assertEqual(s.get(ComputeServer,'compute-3').state,'starting')
            def event(name,**values):
                trace.append(name)
                if name=='container_ready':
                    with self.sessions() as s: self.assertEqual(len(ready_servers(s)),3)
                    autoscaler.stopping=True
                if name=='launch_failed': autoscaler.stopping=True
            def stop(*args,**kwargs): stopped[0]=True;trace.append('owned_processes_stopped')
            proxy=MagicMock(status_code=200);proxy.json.return_value={'server_id':'compute-3'}
            patches = [
                patch.object(autoscaler,'DATA',root),patch.object(autoscaler,'Session',self.sessions),
                patch.object(autoscaler.signal,'signal'),
                patch.object(autoscaler,'reserve_launch',lambda:reserve_launch(self.sessions)),
                patch.object(autoscaler,'launch_failed',lambda t,m:launch_failed(t,m,self.sessions)),
                patch.object(autoscaler,'health',probe),patch.object(autoscaler,'event',event),
                patch.object(autoscaler,'identity',return_value=info),patch.object(autoscaler.httpx,'get',return_value=proxy),
                patch('mtw.control.write_nginx',side_effect=nginx),patch.object(autoscaler.runtime,'ROOT',root),
                patch.object(autoscaler.runtime,'require_root'),patch.object(autoscaler.runtime,'owned_launches',return_value=[]),
                patch.object(autoscaler.runtime,'prepare',side_effect=RuntimeError('fixture bad rootfs') if failure=='prepare' else None),
                patch.object(autoscaler.runtime,'launch',return_value=process),patch.object(autoscaler.runtime,'namespace_of_supervisor',return_value=value['pid_namespace']),
                patch.object(autoscaler.runtime,'supervisor_alive',return_value=True),
                patch.object(autoscaler.runtime,'namespace_members',side_effect=lambda _:[] if stopped[0] else [{'container_pid':4,'argv':'python -m mtw.service compute-3'}]),
                patch.object(autoscaler.runtime,'stop',side_effect=stop),
            ]
            with ExitStack() as stack:
                for context in patches: stack.enter_context(context)
                autoscaler.stopping=False
                try: autoscaler.run()
                finally: autoscaler.stopping=False
            return trace,process

    def test_health_before_nginx_before_ready_and_clean_operator_stop(self):
        trace,process=self.controller()
        self.assertLess(trace.index('health:compute-3'),trace.index('nginx:add'))
        self.assertLess(trace.index('nginx:add'),trace.index('container_ready'))
        self.assertIn('owned_processes_stopped',trace)
        with self.sessions() as s:
            self.assertEqual(s.get(ComputeServer,'compute-3').state,'stopped')
            self.assertEqual(s.get(Job,self.queued).status,'queued')

    def test_bad_environment_never_publishes_or_loses_queued_job(self):
        trace,_=self.controller('prepare')
        self.assertNotIn('nginx:add',trace)
        with self.sessions() as s:
            self.assertEqual(s.get(ComputeServer,'compute-3').state,'failed')
            self.assertEqual(s.get(Job,self.queued).status,'queued')

    def test_proxy_remove_failure_does_not_strand_container_processes(self):
        trace,_=self.controller('nginx-remove')
        self.assertIn('nginx_remove_failed',trace)
        self.assertIn('owned_processes_stopped',trace)
        with self.sessions() as s:
            self.assertEqual(s.get(ComputeServer,'compute-3').state,'stopped')

    def test_start_timeout_stops_only_owned_process_and_keeps_pending_work(self):
        trace,_=self.controller('timeout')
        self.assertNotIn('nginx:add',trace)
        self.assertIn('owned_processes_stopped',trace)
        with self.sessions() as s:
            self.assertEqual(s.get(ComputeServer,'compute-3').state,'failed')
            self.assertEqual(s.get(Job,self.queued).status,'queued')
