"""Real isolated PostgreSQL FIFO/ETA/access tests. No dispatcher, worker or SLAM."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
import secrets
import subprocess
import time
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4
import httpx
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from mtw import api, auth, queue_eta
from mtw.config import database_url
from mtw.db import Base, Job, User, AuthSession, now
from mtw.scheduler import reserve_next


@unittest.skipUnless(os.environ.get("MTW_POSTGRES_TESTS") == "1", "Opt-in isolated PostgreSQL tests")
class QueuePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = "mtw_queue_" + uuid4().hex
        cls.admin = create_engine(database_url())
        with cls.admin.begin() as c:
            c.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
        cls.engine = create_engine(database_url(), connect_args={"options": f"-csearch_path={cls.schema}", "application_name": cls.schema})
        Base.metadata.create_all(cls.engine)
        cls.sessions = sessionmaker(cls.engine, expire_on_commit=False)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        assert cls.schema.startswith("mtw_queue_") and len(cls.schema) == 42
        with cls.admin.begin() as c:
            c.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
        cls.admin.dispose()

    def setUp(self):
        with self.sessions.begin() as s:
            for table in ("jobs", "auth_sessions", "users"):
                s.execute(text(f"DELETE FROM {table}"))
        self.at = now()
        self.enterContext(patch.dict(queue_eta.SETTINGS, initial_seconds=40, reference_frames=360,
            reference_width=640, reference_height=480, history_samples=30))

    def seed(self, number, **values):
        defaults = dict(id=UUID(int=number), parameters={"calibration": {"width": 640, "height": 480}},
            video_info={"frames": 360, "width": 640, "height": 480}, input_ref="queue-test-only",
            created_at=self.at, status="queued", stage="queued")
        with self.sessions.begin() as s:
            s.add(Job(**(defaults | values)))
        return UUID(int=number)

    def estimate(self, job_id):
        with self.sessions() as s:
            return queue_eta.estimate_queue(s, s.get(Job, job_id), self.at)

    def test_fifo_ties_cancelled_excluded_and_two_atomic_slots(self):
        for number in (4, 2, 3, 1):
            self.seed(number)
        self.seed(0, cancel_requested=True)
        self.seed(5, status="cancelled")
        self.seed(6, status="succeeded")
        self.assertEqual(self.estimate(UUID(int=3)), {"queue_position": 3, "estimated_wait_seconds": 40})
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: reserve_next(self.sessions), range(6)))
        claims = [r for r in results if r]
        self.assertEqual({r["job_id"] for r in claims}, {str(UUID(int=1)), str(UUID(int=2))})
        with self.sessions.begin() as s:
            s.get(Job, UUID(int=1)).status = "succeeded"
        self.assertEqual(reserve_next(self.sessions)["job_id"], str(UUID(int=3)))

    def test_locked_fifo_head_not_skipped_and_restart_persists(self):
        self.seed(1)
        self.seed(2)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.sessions.begin() as head:
                head.scalar(select(Job).where(Job.id == UUID(int=1)).with_for_update())
                future = pool.submit(reserve_next, self.sessions)
                deadline, waiting = time.monotonic()+5, False
                while time.monotonic() < deadline:
                    with self.admin.connect() as c:
                        waiting = c.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE application_name=:name AND wait_event_type='Lock')"), {"name":self.schema})
                    if waiting: break
                    self.assertFalse(future.done(), "FIFO head was skipped while locked")
                self.assertTrue(waiting)
            self.assertEqual(future.result(timeout=5)["job_id"],str(UUID(int=1)))
        self.engine.dispose()  # New pool/connections see the committed queue after restart.
        self.assertEqual(reserve_next(self.sessions)["job_id"],str(UUID(int=2)))

    def test_two_lanes_active_remaining_and_work_ahead(self):
        self.seed(10, status="succeeded", execution_seconds=40, total_frames=360, finished_at=self.at)
        self.seed(11, status="running", stage="processing", server_id="compute-1",
            started_at=self.at-timedelta(seconds=30), total_frames=360, processed_frames=270)
        self.seed(12, status="running", stage="processing", server_id="compute-2",
            started_at=self.at-timedelta(seconds=20), total_frames=360, processed_frames=180)
        for n in (1, 2, 3): self.seed(n)
        self.assertEqual([self.estimate(UUID(int=n))["estimated_wait_seconds"] for n in (1,2,3)], [10,20,50])
        with self.sessions.begin() as s:
            s.get(Job, UUID(int=1)).status = "cancelled"
        self.assertEqual(self.estimate(UUID(int=2)), {"queue_position": 1, "estimated_wait_seconds": 10})

    def test_cold_start_scales_by_frames_and_resolution(self):
        self.seed(1, video_info={"frames": 180, "width": 640, "height": 480})
        self.seed(2, video_info={"frames": 360, "width": 320, "height": 240})
        self.seed(3)
        self.assertEqual(self.estimate(UUID(int=3))["estimated_wait_seconds"], 10)

    def test_no_stats_disabled_fallback_and_unknown_feature(self):
        self.seed(1, video_info=None)
        self.seed(2)
        self.seed(3)
        with patch.dict(queue_eta.SETTINGS, initial_seconds=0):
            self.assertEqual(self.estimate(UUID(int=3)), {"queue_position": 3, "estimated_wait_seconds": None})
        self.assertEqual(self.estimate(UUID(int=3))["estimated_wait_seconds"], 40)

    def test_successes_only_queue_time_excluded_and_non_applicable_null(self):
        self.seed(10, status="succeeded", created_at=self.at-timedelta(days=1), execution_seconds=12,
            total_frames=360, finished_at=self.at)
        self.seed(11, status="failed", execution_seconds=9999, total_frames=360)
        for n in (1,2,3): self.seed(n)
        self.assertEqual(self.estimate(UUID(int=3))["estimated_wait_seconds"], 12)
        self.assertEqual(self.estimate(UUID(int=10)), {"queue_position": None, "estimated_wait_seconds": None})
        self.assertEqual(self.estimate(UUID(int=11)), {"queue_position": None, "estimated_wait_seconds": None})

    def test_overdue_unknown_slot_and_remaining_progress_never_negative(self):
        self.seed(10, status="running", stage="initializing", started_at=self.at-timedelta(seconds=100))
        self.seed(11, status="running", stage="processing", started_at=self.at-timedelta(seconds=100),
            total_frames=360, processed_frames=270)
        self.seed(1)
        self.assertEqual(self.estimate(UUID(int=1))["estimated_wait_seconds"], 10)
        with self.sessions.begin() as s:
            s.get(Job, UUID(int=11)).stage = "termination_failed"
        self.assertIsNone(self.estimate(UUID(int=1))["estimated_wait_seconds"])

    def test_owner_only_status_aggregates_no_foreign_identifiers_and_queued_cancel(self):
        token = secrets.token_urlsafe(32)
        with self.sessions.begin() as s:
            a = User(login="queue_a", password_hash="test-session-only")
            b = User(login="queue_b", password_hash="test-session-only")
            s.add_all([a,b]); s.flush()
            aid,bid=a.id,b.id
            s.add(AuthSession(token_hash=auth.token_hash(token), user_id=aid, expires_at=self.at+timedelta(hours=1)))
        self.seed(1, owner_id=bid)
        own = self.seed(2, owner_id=aid)
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="https://127.0.0.1:8443") as c:
                self.assertEqual((await c.get(f"/api/jobs/{own}")).status_code,401)
                c.cookies.set(auth.COOKIE,token)
                response = await c.get(f"/api/jobs/{own}")
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.json()["queue_position"],2)
                self.assertNotIn(str(UUID(int=1)),response.text)
                self.assertNotIn(str(bid),response.text)
                self.assertEqual((await c.get(f"/api/jobs/{UUID(int=1)}")).status_code,404)
                result = await c.post(f"/api/jobs/{own}/cancel", headers={"Origin":"https://127.0.0.1:8443","X-CSRF-Token":auth.csrf_token(token)})
                self.assertEqual(result.status_code,202)
                self.assertEqual(result.json()["status"],"cancelled")
                self.assertIsNone((await c.get(f"/api/jobs/{own}")).json()["queue_position"])
        with patch.object(api,"Session",self.sessions),patch.object(auth,"Session",self.sessions),patch.object(api,"cleanup_decoded_frames"):
            asyncio.run(run())

    def test_header_probe_failure_or_timeout_is_unknown_not_fake_metadata(self):
        with patch.object(queue_eta.subprocess,"run", side_effect=subprocess.TimeoutExpired("ffprobe",2)):
            self.assertIsNone(queue_eta.probe_video("isolated-file"))
        with patch.object(queue_eta.subprocess,"run", return_value=subprocess.CompletedProcess([],0,'{"streams":[{"width":640,"height":480,"nb_frames":"300"}]}')):
            self.assertEqual(queue_eta.probe_video("isolated-file")["frames"],300)
