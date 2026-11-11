"""Disposable PostgreSQL admin/auth/CLI tests; no dispatcher, container or SLAM."""
import asyncio
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4,UUID
import httpx
from sqlalchemy import create_engine,select,text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from mtw import api,auth,admin,monitoring
from mtw.admin_role import set_role
from mtw.config import CONFIG,CONFIG_PATH,DATA,REPO,database_url
from mtw.db import Base,User,Job,ComputeServer,now
from mtw.servers import seed_test_baseline
from test_contracts import CALIBRATION


@unittest.skipUnless(os.environ.get('MTW_POSTGRES_TESTS')=='1','Opt-in isolated PostgreSQL')
class AdminPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema='mtw_admin_'+uuid4().hex;cls.parent=create_engine(database_url())
        with cls.parent.begin() as c:c.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
        cls.engine=create_engine(database_url(),connect_args={'options':f'-csearch_path={cls.schema}'})
        Base.metadata.create_all(cls.engine);cls.sessions=sessionmaker(cls.engine,expire_on_commit=False)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose();assert cls.schema.startswith('mtw_admin_') and len(cls.schema)==42
        with cls.parent.begin() as c:c.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
        cls.parent.dispose()

    def setUp(self):
        with self.sessions.begin() as s:
            for table in ('jobs','auth_sessions','users','compute_servers'):s.execute(text('DELETE FROM '+table))
        self.tmp=tempfile.TemporaryDirectory(dir=DATA/'reports',prefix='admin-unit-');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'ui').mkdir();(self.root/'ui/index.html').write_text('<html>admin shell</html>')
        self.enterContext(patch.object(auth,'Session',self.sessions));self.enterContext(patch.object(api,'Session',self.sessions))
        self.enterContext(patch.object(admin,'DATA',self.root))
        self.password=secrets.token_urlsafe(24)

    def client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='https://127.0.0.1:8443',headers={'Origin':'https://127.0.0.1:8443'})

    async def register(self,c,login):
        r=await c.get('/api/auth/session');c.headers['X-CSRF-Token']=r.json()['csrf_token']
        r=await c.post('/api/auth/register',json={'login':login,'password':self.password})
        self.assertEqual(r.status_code,201);c.headers['X-CSRF-Token']=r.json()['csrf_token']
        return r.json()['user']

    def cli(self,*args):
        (self.root/'database-url').write_text(make_url(database_url()).update_query_dict({'options':f'-csearch_path={self.schema}'}).render_as_string(hide_password=False))
        (self.root/'database-url').chmod(0o600)
        config=CONFIG_PATH.read_text().replace(f'data_root = "{CONFIG["paths"]["data_root"]}"',f'data_root = "{self.root}"')
        path=self.root/'local.toml';path.write_text(config)
        return subprocess.run(['bash',str(REPO/'scripts/admin-role'),*args],env=os.environ|{'MTW_CONFIG':str(path)},capture_output=True,text=True,timeout=10)

    def test_anonymous_ordinary_admin_revoke_and_existing_session(self):
        async def run():
            async with self.client() as c:
                for path in ('/admin','/admin/','/api/admin/monitoring'):
                    self.assertEqual((await c.get(path)).status_code,401)
                user=await self.register(c,'explicit_admin')
                self.assertFalse(user['is_admin'])
                with patch.object(admin,'cached_snapshot',side_effect=AssertionError('Denied before metrics read')):
                    for path in ('/admin','/api/admin/monitoring'):self.assertEqual((await c.get(path)).status_code,403)
                result=self.cli('--login','explicit_admin','--grant');self.assertEqual(result.returncode,0)
                for path in ('/admin','/admin/','/api/admin/monitoring'):self.assertEqual((await c.get(path)).status_code,200)
                self.assertTrue((await c.get('/api/auth/me')).json()['is_admin'])
                self.assertEqual(self.cli('--login','explicit_admin','--revoke').returncode,0)
                self.assertEqual((await c.get('/api/admin/monitoring')).status_code,403)
        asyncio.run(run())

    def test_registration_cannot_assign_role_and_cli_requires_existing_login(self):
        async def run():
            async with self.client() as c:
                await self.register(c,'ordinary_first')
                for field in ('is_admin','role'):
                    response=await c.post('/api/auth/register',json={'login':'spoofer','password':self.password,field:True})
                    self.assertEqual(response.status_code,422)
                self.assertEqual((await c.get('/api/auth/me')).json()['is_admin'],False)
                r=await c.post('/api/jobs',files={'video':('video.mp4',b'fixture','video/mp4')},
                    data={'parameters':json.dumps({'calibration':CALIBRATION,'is_admin':True})})
                self.assertEqual(r.status_code,422)
                self.assertFalse((await c.get('/api/auth/me')).json()['is_admin'])
        asyncio.run(run())
        self.assertNotEqual(self.cli('--grant').returncode,0)
        self.assertNotEqual(self.cli('--login','missing','--grant').returncode,0)
        with self.sessions() as s:self.assertEqual(len(list(s.scalars(select(User).where(User.is_admin)))),0)

    def test_admin_does_not_bypass_ownership_or_enable_mutations(self):
        async def run():
            async with self.client() as a,self.client() as b:
                ua=await self.register(a,'administrator');ub=await self.register(b,'owner_only')
                set_role(ua['login'],True,self.sessions)
                with self.sessions.begin() as s:
                    job=Job(owner_id=UUID(ub['id']),parameters={},input_ref='private',status='succeeded');s.add(job);s.flush();job_id=job.id
                for tail in ('','/snapshot','/result','/video','/frames/0','/artifacts/trajectory.json'):
                    self.assertEqual((await a.get(f'/api/jobs/{job_id}{tail}')).status_code,404)
                self.assertEqual((await a.post(f'/api/jobs/{job_id}/cancel')).status_code,404)
                self.assertEqual((await a.post('/api/admin/monitoring',json={'command':'do not execute'})).status_code,405)
        asyncio.run(run())

    def test_queue_slots_unreachable_server_and_no_sensitive_process_fields(self):
        with self.sessions.begin() as s:
            seed_test_baseline(s);s.add(ComputeServer(server_id='compute-3',kind='nspawn',state='stopped'))
            for i in (1,2):s.add(Job(server_id=f'compute-{i}',status='running' if i==1 else 'dispatching',parameters={},input_ref='must-not-leak',started_at=now() if i==1 else None))
            for _ in range(2):s.add(Job(parameters={},input_ref='not-serialized'))
        collector=monitoring.Collector(self.sessions)
        self.addCleanup(collector.client.close)
        with patch.object(collector,'health',side_effect=lambda n:'unavailable' if n=='compute-2' else 'healthy'):
            value=collector.collect()
        self.assertEqual([j['queue_position'] for j in value['queue']],[1,2])
        self.assertEqual(value['capacity']['active'],2)
        self.assertEqual(value['servers'][1]['health'],'unavailable')
        self.assertTrue(all(s['slot_used'] for s in value['servers'][:2]))
        self.assertEqual(value['servers'][1]['job_state'],'dispatching')
        self.assertTrue(all(j['estimated_wait_seconds'] is None or j['estimated_wait_seconds']>=0 for j in value['queue']))
        encoded=json.dumps(value)
        for secret in ('_argv','password_hash','token_hash','database-url','must-not-leak','not-serialized'):
            self.assertNotIn(secret,encoded)

    def test_empty_queue_and_missing_service_are_explicit(self):
        collector=monitoring.Collector(self.sessions);self.addCleanup(collector.client.close)
        with patch.object(monitoring,'DATA',self.root),patch.object(collector,'health',return_value='unavailable'):
            value=collector.collect()
        self.assertEqual(value['queue'],[])
        self.assertEqual(value['queue_status'],'healthy')
        self.assertFalse(next(s for s in value['services'] if s['name']=='dispatcher')['running'])
