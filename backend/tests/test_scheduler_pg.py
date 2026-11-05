"""Opt-in concurrency tests against an isolated schema in the real PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
import os
import unittest
from uuid import uuid4
from unittest.mock import patch
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from mtw.config import database_url
from mtw.db import Base, Job
from mtw.scheduler import outstanding, reserve_next, still_reserved
from mtw.servers import seed_test_baseline


@unittest.skipUnless(os.environ.get("MTW_POSTGRES_TESTS") == "1", "Opt-in isolated PostgreSQL tests")
class SchedulerPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = "mtw_test_" + uuid4().hex
        cls.admin = create_engine(database_url())
        with cls.admin.begin() as c:
            c.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
        cls.engine = create_engine(database_url(), pool_size=10,
            connect_args={"options": f"-csearch_path={cls.schema}"})
        cls.sessions = sessionmaker(cls.engine, expire_on_commit=False)
        Base.metadata.create_all(cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        assert cls.schema.startswith("mtw_test_") and len(cls.schema) == 41
        with cls.admin.begin() as c:
            c.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
        cls.admin.dispose()

    def setUp(self):
        with self.sessions.begin() as s:
            s.execute(text("DELETE FROM jobs"))
            s.execute(text("DELETE FROM compute_servers"))
            seed_test_baseline(s)
            for _ in range(3):
                s.add(Job(parameters={}, input_ref="test-only"))

    def test_atomic_claim_global_cap_and_queue(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda _: reserve_next(self.sessions), range(8)))
        claimed = [c for c in claims if c]
        self.assertEqual(len(claimed), 2)
        self.assertEqual(len({c["job_id"] for c in claimed}), 2)
        with self.sessions() as s:
            self.assertEqual(sorted(s.scalars(select(Job.status))), ["dispatching", "dispatching", "queued"])

    def test_failure_and_timeout_release_reservation(self):
        for terminal in ("failed", "timed_out", "cancelled", "succeeded"):
            with self.subTest(terminal=terminal):
                self.setUp()
                first = reserve_next(self.sessions)
                reserve_next(self.sessions)
                self.assertIsNone(reserve_next(self.sessions))
                with self.sessions.begin() as s:
                    s.get(Job, first["job_id"]).status = terminal
                self.assertIsNotNone(reserve_next(self.sessions))

    def test_restart_retains_token_running_job_never_reclaimed(self):
        first = reserve_next(self.sessions)
        self.assertEqual(outstanding(self.sessions), [first])
        self.assertTrue(still_reserved(first, self.sessions))
        with self.sessions.begin() as s:
            j = s.get(Job, first["job_id"])
            j.status, j.server_id = "running", "compute-1"
        second = reserve_next(self.sessions)
        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertFalse(still_reserved(first, self.sessions))
        self.assertEqual(outstanding(self.sessions), [second])
        self.assertIsNone(reserve_next(self.sessions))

    def test_database_rejects_second_active_job_on_same_server(self):
        first, second = reserve_next(self.sessions), reserve_next(self.sessions)
        with self.sessions.begin() as s:
            j = s.get(Job, first["job_id"])
            j.status, j.server_id = "running", "compute-1"
        with self.assertRaises(IntegrityError), self.sessions.begin() as s:
            j = s.get(Job, second["job_id"])
            j.status, j.server_id = "running", "compute-1"

    def test_cancel_unaccepted_reservation_blocks_late_delivery(self):
        from mtw import api
        from mtw.auth import hasher
        from mtw.db import User
        from starlette.requests import Request
        import secrets
        reservation = reserve_next(self.sessions)
        with self.sessions.begin() as s:
            user = User(login="scheduler_fixture", password_hash=hasher.hash(secrets.token_urlsafe(24)))
            s.add(user)
            s.flush()
            s.get(Job, reservation["job_id"]).owner_id = user.id
            owner_id = str(user.id)
        request = Request({"type": "http", "state": {"auth": {"user": {"id": owner_id}}}})
        # Only the isolated schema is used; a nonexistent preview directory has
        # no cleanup marker and cannot affect any saved job.
        with patch.object(api, "Session", self.sessions):
            result = api.cancel(reservation["job_id"], request)
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(still_reserved(reservation, self.sessions))
        self.assertEqual(outstanding(self.sessions), [])
        self.assertIsNotNone(reserve_next(self.sessions))
