"""Real PostgreSQL slot assignment, delivery and lifecycle; no SLAM or production rows."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import timedelta
import importlib
import io
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4
import httpx
from sqlalchemy import select, text
from mtw import api, dispatcher, queue_eta, servers
from mtw.db import ComputeServer, Job, User, now
from mtw.scheduler import outstanding, reserve_next, still_reserved
import test_scheduler_pg as fixture


@unittest.skipUnless(os.environ.get('MTW_POSTGRES_TESTS') == '1', 'Explicit isolated PostgreSQL')
class LoadBalancingTests(unittest.TestCase):
    setUpClass = classmethod(fixture.SchedulerPostgresTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.SchedulerPostgresTests.tearDownClass.__func__)
    setUp = fixture.SchedulerPostgresTests.setUp

    def occupy(self, server, status='running'):
        with self.sessions.begin() as s:
            job = s.scalar(select(Job).where(Job.status == 'queued').order_by(Job.created_at, Job.id).limit(1))
            job.status = job.stage = status
            job.server_id, job.dispatch_token = server, uuid4()
            job.parameters = {'execution_seconds': 180}
            return job.id

    def test_busy_first_selects_second_and_busy_second_selects_first(self):
        for busy, free in [('compute-1', 'compute-2'), ('compute-2', 'compute-1')]:
            with self.subTest(busy=busy):
                self.setUp();self.occupy(busy)
                claim = reserve_next(self.sessions)
                self.assertEqual(claim['server_id'], free)
                with self.sessions() as s:self.assertEqual(s.get(Job, claim['job_id']).server_id, free)

    def test_both_busy_leave_fifo_head_queued(self):
        self.occupy('compute-1');self.occupy('compute-2')
        with self.sessions() as s:head = s.scalar(select(Job.id).where(Job.status == 'queued'))
        self.assertIsNone(reserve_next(self.sessions))
        with self.sessions() as s:self.assertEqual(s.get(Job, head).status, 'queued')

    def test_dispatching_is_an_occupied_slot(self):
        self.occupy('compute-1', 'dispatching')
        self.assertEqual(reserve_next(self.sessions)['server_id'], 'compute-2')
        self.assertIsNone(reserve_next(self.sessions))

    def test_stopped_and_stale_servers_are_excluded(self):
        for state, heartbeat in [('stopped', now()), ('ready', now()-timedelta(seconds=100))]:
            with self.subTest(state=state):
                self.setUp()
                with self.sessions.begin() as s:
                    row=s.get(ComputeServer,'compute-1');row.state=state;row.heartbeat_at=heartbeat
                self.assertEqual(reserve_next(self.sessions)['server_id'], 'compute-2')

    def test_no_ready_servers_logs_the_actual_wait_reason(self):
        with self.sessions.begin() as s:s.execute(text("UPDATE compute_servers SET state='stopped'"))
        output=io.StringIO()
        with redirect_stdout(output):self.assertIsNone(reserve_next(self.sessions))
        self.assertEqual(json.loads(output.getvalue())['reason'],'no_fresh_ready_servers')

    def test_ready_third_participates_and_maximum_three(self):
        with self.sessions.begin() as s:s.add(ComputeServer(server_id='compute-3',kind='nspawn',state='ready',heartbeat_at=now()))
        with ThreadPoolExecutor(max_workers=12) as pool:
            claims=[c for c in pool.map(lambda _:reserve_next(self.sessions), range(12)) if c]
        self.assertEqual({c['server_id'] for c in claims}, set(servers.SERVERS))
        self.assertEqual(len({c['job_id'] for c in claims}), 3)
        self.assertIsNone(reserve_next(self.sessions))

    def test_same_free_servers_alternate_fairly_and_restart_keeps_assignment(self):
        choices=[]
        for _ in range(8):
            claim=reserve_next(self.sessions);choices.append(claim['server_id'])
            self.engine.dispose()
            self.assertIn(claim,outstanding(self.sessions));self.assertTrue(still_reserved(claim,self.sessions))
            with self.sessions.begin() as s:
                s.get(Job,claim['job_id']).status='succeeded'
                s.add(Job(parameters={},input_ref='fairness-only'))
        self.assertEqual(choices,['compute-1','compute-2']*4)

    def test_legacy_without_assignment_blocks_and_cancel_cannot_release_ambiguous_launch(self):
        from starlette.requests import Request
        for n,server in enumerate((None,'unknown-compute')):
            with self.subTest(server=server):
                self.setUp();legacy=self.occupy(server,'dispatching')
                self.assertIsNone(reserve_next(self.sessions));self.assertEqual(outstanding(self.sessions),[])
                with self.sessions.begin() as s:
                    user=User(login=f'legacy_owner_{n}',password_hash='fixture-only');s.add(user);s.flush()
                    s.get(Job,legacy).owner_id=user.id;owner=str(user.id)
                request=Request({'type':'http','state':{'auth':{'user':{'id':owner}}}})
                with patch.object(api,'Session',self.sessions):result=api.cancel(legacy,request)
                self.assertEqual(result['status'],'dispatching');self.assertTrue(result['cancel_requested'])
                self.assertEqual(result['stage'],'assignment_unresolved')
                self.assertIsNone(reserve_next(self.sessions))

    def test_decision_log_contains_actual_loads_and_no_token(self):
        self.occupy('compute-1','dispatching');output=io.StringIO()
        with redirect_stdout(output):claim=reserve_next(self.sessions)
        decision=json.loads(output.getvalue())
        self.assertEqual(decision['server_id'],'compute-2')
        c={r['server_id']:r for r in decision['candidates']}
        self.assertEqual((c['compute-1']['reserved'],c['compute-1']['load']),(1,1))
        self.assertEqual((c['compute-2']['running'],c['compute-2']['capacity'],c['compute-2']['load']),(0,1,0))
        self.assertNotIn(claim['dispatch_token'],output.getvalue())
        self.assertNotIn('service-key',output.getvalue())

    def test_retry_ambiguous_response_uses_same_server_token_after_restart(self):
        claim=reserve_next(self.sessions);seen=[];stop=asyncio.Event()
        def transport(request):
            seen.append((str(request.url),json.loads(request.content)))
            if len(seen)==1:raise httpx.ReadTimeout('accepted response lost',request=request)
            stop.set();return httpx.Response(202)
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
                with patch.object(dispatcher,'still_reserved',lambda r:still_reserved(r,self.sessions)), \
                        patch.object(dispatcher,'service_headers',return_value={}):
                    await dispatcher.deliver(client,outstanding(self.sessions)[0],stop)
        asyncio.run(run())
        self.assertEqual(len(seen),2);self.assertEqual(seen[0],seen[1])
        self.assertTrue(seen[0][0].endswith(servers.compute_route(claim['server_id'])))
        with self.sessions.begin() as s:
            s.get(ComputeServer,claim['server_id']).state='stopped'
        self.assertEqual(outstanding(self.sessions),[claim])

    def test_eta_respects_assigned_lanes_and_offline_reservations(self):
        busy=self.occupy('compute-1','dispatching')
        with self.sessions() as s:
            head=s.scalar(select(Job).where(Job.status=='queued').order_by(Job.created_at))
            self.assertEqual(queue_eta.estimate_queue(s,head)['estimated_wait_seconds'],0)
        with self.sessions.begin() as s:s.get(ComputeServer,'compute-1').state='stopped'
        with self.sessions() as s:
            head=s.scalar(select(Job).where(Job.status=='queued').order_by(Job.created_at))
            self.assertEqual(queue_eta.estimate_queue(s,head)['estimated_wait_seconds'],0)
        self.assertEqual(reserve_next(self.sessions)['server_id'],'compute-2')

    def test_two_reservations_trigger_autoscaler_without_starting_any_container(self):
        with self.sessions.begin() as s:s.add(ComputeServer(server_id='compute-3',kind='nspawn',state='stopped'))
        reserve_next(self.sessions);reserve_next(self.sessions)
        self.assertIsNotNone(servers.reserve_launch(self.sessions))
        self.assertIsNone(servers.reserve_launch(self.sessions))

    def test_compute_rejects_wrong_server_and_duplicate_starts_one_manager(self):
        os.environ.setdefault('MTW_SERVER_ID','compute-1');compute=importlib.import_module('mtw.compute')
        claim=reserve_next(self.sessions)
        with self.sessions.begin() as s:s.get(Job,claim['job_id']).parameters={'execution_seconds':180}
        calls=[]
        async def run():
            done=asyncio.Event()
            async def manager(job_id,budget):
                calls.append(str(job_id));await done.wait();compute.slot.release()
            with patch.object(compute,'Session',self.sessions),patch.object(compute,'slot',asyncio.Lock()), \
                    patch.object(compute,'active',None),patch.object(compute,'manager_task',None), \
                    patch.object(compute,'run_job',manager),patch.object(compute,'job_dir',lambda _:Path('/nonexistent-load-test')):
                payload=compute.Execution(job_id=claim['job_id'],dispatch_token=claim['dispatch_token'])
                with patch.object(compute,'SERVER_ID','compute-2'):
                    with self.assertRaises(Exception) as error:await compute.execute(payload)
                    self.assertEqual(error.exception.status_code,409)
                try:
                    first=await compute.execute(payload);duplicate=await compute.execute(payload)
                    await asyncio.sleep(0)
                    self.assertEqual(first.status_code,202);self.assertEqual(duplicate.status_code,202)
                    self.assertTrue(json.loads(duplicate.body)['duplicate']);self.assertEqual(calls,[claim['job_id']])
                finally:
                    done.set()
                    if compute.manager_task:await compute.manager_task
        asyncio.run(run())

    def test_actual_spawn_failure_clears_only_its_slot_and_keeps_neighbour(self):
        os.environ.setdefault('MTW_SERVER_ID','compute-1');compute=importlib.import_module('mtw.compute')
        target=reserve_next(self.sessions);neighbour=reserve_next(self.sessions)
        with self.sessions.begin() as s:s.get(Job,target['job_id']).status='running'
        with tempfile.TemporaryDirectory() as directory:
            async def run():
                lock=asyncio.Lock();await lock.acquire()
                with patch.object(compute,'Session',self.sessions),patch.object(compute,'slot',lock), \
                        patch.object(compute,'active',target['job_id']),patch.object(compute,'job_dir',lambda _:Path(directory)), \
                        patch.object(compute.asyncio,'create_subprocess_exec',AsyncMock(side_effect=OSError('fixture spawn failed'))):
                    await compute.run_job(UUID(target['job_id']),180)
                    self.assertFalse(lock.locked())
            asyncio.run(run())
            self.assertEqual(json.loads((Path(directory)/'termination.json').read_text())['group_members_after'],[])
        with self.sessions() as s:
            self.assertEqual(s.get(Job,target['job_id']).status,'failed')
            self.assertEqual(s.get(Job,neighbour['job_id']).status,'dispatching')
        self.assertEqual(reserve_next(self.sessions)['server_id'],target['server_id'])

    def test_route_allowlist_rejects_arbitrary_host_or_path(self):
        for name in ('compute-4','http://example.com','../compute-1'):
            with self.assertRaises(ValueError):servers.compute_route(name)

    def test_real_process_cancel_and_timeout_release_only_target_group(self):
        """Real OS groups, blocked on signals; no video/SLAM or fake progress."""
        os.environ.setdefault('MTW_SERVER_ID','compute-1');compute=importlib.import_module('mtw.compute')
        real_spawn=asyncio.create_subprocess_exec
        for mode in ('cancel','timeout'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                self.setUp();target=reserve_next(self.sessions);other=reserve_next(self.sessions)
                with self.sessions.begin() as s:
                    s.get(Job,target['job_id']).status='running';s.get(Job,other['job_id']).status='running'
                async def run():
                    neighbour=await real_spawn(sys.executable,'-c','import signal;signal.pause()',start_new_session=True)
                    spawned=asyncio.Event();lock=asyncio.Lock();await lock.acquire()
                    async def spawn(*args,**kwargs):
                        child=await real_spawn(sys.executable,'-c','import signal;signal.pause()',
                            'mtw.worker',target['job_id'],start_new_session=True,stdout=kwargs['stdout'],stderr=kwargs['stderr'])
                        spawned.set();return child
                    async def cancel():
                        await spawned.wait()
                        with self.sessions.begin() as s:s.get(Job,target['job_id']).cancel_requested=True
                    cancel_task=asyncio.create_task(cancel()) if mode=='cancel' else None
                    try:
                        with self.engine.connect() as lease,patch.object(compute,'lease',lease), \
                                patch.object(compute,'shutdown',asyncio.Event()), \
                                patch.object(compute,'Session',self.sessions),patch.object(compute,'slot',lock), \
                                patch.object(compute,'active',target['job_id']),patch.object(compute,'job_dir',lambda _:Path(directory)), \
                                patch.object(compute.asyncio,'create_subprocess_exec',spawn),patch.object(compute,'ensure_space'):
                            await compute.run_job(UUID(target['job_id']),.05 if mode=='timeout' else 5)
                        self.assertFalse(lock.locked());self.assertIsNone(neighbour.returncode)
                    finally:
                        if cancel_task:await cancel_task
                        neighbour.terminate();await neighbour.wait()
                asyncio.run(run())
                with self.sessions() as s:
                    self.assertEqual(s.get(Job,target['job_id']).status,'cancelled' if mode=='cancel' else 'timed_out')
                    self.assertEqual(s.get(Job,other['job_id']).status,'running')
                proof=json.loads((Path(directory)/'termination.json').read_text())
                self.assertEqual(proof['group_members_after'],[])
                self.assertEqual(reserve_next(self.sessions)['server_id'],target['server_id'])

    def test_compute_restart_reconciles_running_before_freeing_slot(self):
        os.environ.setdefault('MTW_SERVER_ID','compute-1');compute=importlib.import_module('mtw.compute')
        job_id=self.occupy('compute-1')
        with self.sessions.begin() as s:
            job=s.get(Job,job_id);job.worker_pid=123456;job.worker_start_ticks='fixture-start'
        trace=[]
        async def terminate(*_):trace.append('terminate')
        async def reap(*_):trace.append('reap')
        def cleanup(*_):
            trace.append('cleanup')
            with self.sessions() as s:self.assertEqual(s.get(Job,job_id).status,'running')
        async def run():
            with patch.object(compute,'Session',self.sessions),patch.object(compute,'engine',MagicMock()), \
                    patch.object(compute,'shutdown',asyncio.Event()), \
                    patch.object(compute,'subreaper'),patch.object(compute,'terminate_group',terminate), \
                    patch.object(compute,'reap_group',reap),patch.object(compute,'cleanup_decoded_frames',cleanup), \
                    patch.object(compute,'manager_task',None):
                async with compute.lifespan(compute.app):
                    with self.sessions() as s:self.assertEqual(s.get(Job,job_id).status,'failed')
        asyncio.run(run());self.assertEqual(trace,['terminate','reap','cleanup'])

    def test_compute_restart_preserves_unaccepted_server_and_token(self):
        os.environ.setdefault('MTW_SERVER_ID','compute-1');compute=importlib.import_module('mtw.compute')
        claim=reserve_next(self.sessions)
        async def run():
            with patch.object(compute,'Session',self.sessions),patch.object(compute,'engine',MagicMock()), \
                    patch.object(compute,'shutdown',asyncio.Event()), \
                    patch.object(compute,'subreaper'),patch.object(compute,'manager_task',None):
                async with compute.lifespan(compute.app):self.assertEqual(outstanding(self.sessions),[claim])
        asyncio.run(run());self.assertTrue(still_reserved(claim,self.sessions))

    def test_missing_launch_identity_holds_slot_on_recovery_failure(self):
        os.environ.setdefault('MTW_SERVER_ID','compute-1');compute=importlib.import_module('mtw.compute')
        job_id=self.occupy('compute-1')
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'execution-start.json').write_text('{}')
            async def run():
                with patch.object(compute,'Session',self.sessions),patch.object(compute,'engine',MagicMock()), \
                        patch.object(compute,'shutdown',asyncio.Event()), \
                        patch.object(compute,'subreaper'),patch.object(compute,'manager_task',None), \
                        patch.object(compute,'job_dir',lambda _:Path(directory)):
                    with self.assertRaisesRegex(RuntimeError,'Unresolved launch identity'):
                        async with compute.lifespan(compute.app):pass
            asyncio.run(run())
        with self.sessions() as s:self.assertEqual(s.get(Job,job_id).status,'running')
