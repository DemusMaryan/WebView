"""R1 admission and real decode-failure cleanup in an isolated PG schema.
No dispatcher, mono_export or SLAM is started by these tests.
"""
import asyncio
import importlib
import json
import os
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
from datetime import timedelta
import av
from PIL import Image
import httpx
from sqlalchemy import create_engine,text,select
from sqlalchemy.orm import sessionmaker
from mtw import api,auth,frame_work
from mtw.config import DATA,LIMITS,database_url
from mtw.contracts import JobParameters
from mtw.db import Base,Job,User,AuthSession,now
from mtw.files import read_json,atomic_json
from mtw.processes import identity
from mtw.video import decode
from test_contracts import CALIBRATION


@unittest.skipUnless(os.environ.get('MTW_POSTGRES_TESTS')=='1','Explicit isolated PostgreSQL')
class FinalInputTests(unittest.TestCase):
    evidence=[]
    @classmethod
    def setUpClass(cls):
        cls.schema='mtw_final_'+uuid4().hex;cls.admin=create_engine(database_url())
        with cls.admin.begin() as c:c.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
        cls.engine=create_engine(database_url(),connect_args={'options':f'-csearch_path={cls.schema}'})
        Base.metadata.create_all(cls.engine);cls.sessions=sessionmaker(cls.engine,expire_on_commit=False)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose();assert cls.schema.startswith('mtw_final_') and len(cls.schema)==42
        with cls.admin.begin() as c:c.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
        cls.admin.dispose()
        if os.environ.get('MTW_FINAL_REPORT'):
            output=Path(os.environ['MTW_FINAL_REPORT']).resolve()
            assert output.parent.parent==(DATA/'reports').resolve() and output.parent.name.startswith('final-')
            atomic_json(output,{'schema_removed':True,'SLAM_runs':0,'cases':cls.evidence})

    def setUp(self):
        with self.sessions.begin() as s:
            for table in ('jobs','auth_sessions','users'):s.execute(text('DELETE FROM '+table))
            user=User(login='r1_fixture',password_hash='not-a-login-fixture');s.add(user);s.flush();self.owner=user.id
            token=secrets.token_urlsafe(32)
            s.add(AuthSession(token_hash=auth.token_hash(token),user_id=user.id,expires_at=now()+timedelta(minutes=5)))
        self.headers={'Cookie':f'{auth.COOKIE}={token}','Origin':'https://127.0.0.1:8443','X-CSRF-Token':auth.csrf_token(token)}
        tmp=tempfile.TemporaryDirectory(prefix='final-input-',dir=DATA/'reports');self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);(self.root/'jobs').mkdir()
        self.enterContext(patch.object(api,'Session',self.sessions));self.enterContext(patch.object(auth,'Session',self.sessions))
        self.enterContext(patch.object(api,'job_dir',lambda value:self.root/'jobs'/str(value)))
        self.enterContext(patch.object(frame_work,'DATA',self.root))

    def request(self,**kwargs):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='https://127.0.0.1:8443',headers=self.headers) as c:
                return await c.post('/api/jobs',**kwargs)
        return asyncio.run(run())

    def upload(self,content,**params):
        return self.request(files={'video':('input.mp4',content,'video/mp4')},data={'parameters':json.dumps({'calibration':CALIBRATION}|params)})

    def video(self):
        path=self.root/'fixture.mp4'
        with av.open(str(path),'w') as c:
            stream=c.add_stream('libx264',rate=30);stream.width=64;stream.height=48;stream.pix_fmt='yuv420p'
            for i in range(3):
                for packet in stream.encode(av.VideoFrame.from_image(Image.new('RGB',(64,48),(20*i,30,50)))):c.mux(packet)
            for packet in stream.encode():c.mux(packet)
        return path.read_bytes()

    def test_valid_exact_input_boundaries_admit_and_decode(self):
        content=self.video()
        r=self.upload(content,file_bytes=len(content),frames=3,width=64,height=48,duration_seconds=.1,execution_seconds=5)
        self.assertEqual(r.status_code,202,r.text);directory=self.root/'jobs'/r.json()['id']
        params=JobParameters.model_validate_json((directory/'parameters.json').read_text())
        meta=decode(directory/'input.video',directory,params)
        self.assertEqual((meta['frames'],meta['width'],meta['height']),(3,64,48))
        cleanup=frame_work.cleanup_decoded_frames(directory)
        self.assertFalse((directory/'sequence/rgb').exists());self.assertTrue((directory/'input.video').exists())
        self.evidence.append({'case':'valid exact bytes/frame/dimensions/duration','http':202,'decoded_frames':3,'metadata':meta,'cleanup':cleanup})

    def test_parameters_cannot_raise_server_limits_and_bad_calibration_is_clear(self):
        for key in ('file_bytes','frames','width','height','duration_seconds','execution_seconds'):
            with self.subTest(key=key):
                r=self.upload(b'x',**{key:LIMITS[key]+1})
                self.assertEqual(r.status_code,422,r.text)
                self.assertIn(key,r.text)
                self.evidence.append({'case':'parameter above configured '+key,'http':r.status_code,'detail':r.json()['detail']})
        for cal in (None,CALIBRATION|{'fx':0},CALIBRATION|{'cx':64}):
            r=self.upload(b'x',calibration=cal);self.assertEqual(r.status_code,422)
        self.assertEqual(list((self.root/'jobs').iterdir()),[])

    def test_upload_rejections_remove_partial_files_and_records(self):
        r=self.upload(b'x'*(1048576+2),file_bytes=1048577)
        self.assertEqual(r.status_code,413);self.assertIn('file_bytes',r.text)
        self.assertEqual(list((self.root/'jobs').iterdir()),[])
        self.assertEqual(self.upload(b'').status_code,422)
        with self.sessions() as s:self.assertEqual(list(s.scalars(select(Job))),[])
        self.evidence.append({'case':'partial upload/empty input','http':413,'message':r.json()['detail'],'upload_directories_after':0})

    def test_global_body_cap_rejects_content_length_and_chunked_stream(self):
        boundary='final-r1-boundary'
        prefix=(f'--{boundary}\r\nContent-Disposition: form-data; name="parameters"\r\n\r\n'+json.dumps({'calibration':CALIBRATION})+
            f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="video"; filename="input.mp4"\r\nContent-Type: video/mp4\r\n\r\n').encode()
        async def chunks():
            yield prefix
            for _ in range(5):yield b'x'*16384
            yield f'\r\n--{boundary}--\r\n'.encode()
        headers={'Content-Type':f'multipart/form-data; boundary={boundary}'}
        with patch.dict(api.LIMITS,file_bytes=32):
            for label,body in [('content_length',b'x'*70000),('chunked',chunks())]:
                r=self.request(content=body,headers=headers)
                self.assertEqual(r.status_code,413,r.text)
                self.evidence.append({'case':label+' total body > file cap+65536','http':413,'message':r.json()['detail']})
        self.assertEqual(list((self.root/'jobs').iterdir()),[])

    def test_real_worker_decode_errors_reap_process_and_clean_frames_without_SLAM(self):
        os.environ.setdefault('MTW_SERVER_ID','compute-1');compute=importlib.import_module('mtw.compute')
        cases=[('frames',{'frames':2},'frame count'),('duration',{'duration_seconds':.04},'duration'),
            ('width',{'width':32},'resolution'),('height',{'height':24},'resolution'),
            ('calibration',{'calibration':CALIBRATION|{'width':63}},'explicit calibration'),('corrupt',{},'InvalidDataError')]
        for label,params,expected in cases:
            with self.subTest(label=label):
                content=b'not a video' if label=='corrupt' else self.video()
                response=self.upload(content,execution_seconds=5,**params);self.assertEqual(response.status_code,202,response.text)
                job_id=response.json()['id'];directory=self.root/'jobs'/job_id
                with self.sessions.begin() as s:
                    job=s.get(Job,job_id);job.status=job.stage='running';job.server_id='compute-1';job.started_at=now()
                with self.engine.connect() as lease,patch.object(compute,'lease',lease), \
                        patch.object(compute,'Session',self.sessions),patch.object(compute,'job_dir',lambda value:self.root/'jobs'/str(value)), \
                        patch.object(compute,'slot',asyncio.Lock()),patch.object(compute,'active',job_id):
                    async def run():
                        await compute.slot.acquire();await compute.run_job(job_id,5)
                    asyncio.run(run());self.assertFalse(compute.slot.locked())
                with self.sessions() as s:
                    job=s.get(Job,job_id);self.assertEqual(job.status,'failed',job.error);self.assertIn(expected,job.error)
                proof=read_json(directory/'termination.json')
                self.assertEqual(proof['group_members_after'],[]);self.assertIsNone(identity(proof['worker_pid']))
                self.assertFalse((directory/'sequence/rgb').exists());self.assertFalse((directory/'native').exists())
                self.assertTrue((directory/'input.video').exists())
                self.evidence.append({'case':'real decode '+label,'status':job.status,'error':job.error,
                    'termination':proof,'no_native_started':True,'RGB_work_removed':True})
