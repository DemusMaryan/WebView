"""Real HTTP admission/pagination contracts in a disposable PostgreSQL schema.

No dispatcher, decoder or SLAM is started. Upload bytes are contract fixtures.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import secrets
from pathlib import Path
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import sessionmaker
from mtw import api, auth, frame_work
from mtw.config import DATA, database_url
from mtw.db import AuthSession, Base, Job, User, now
from test_contracts import CALIBRATION


@unittest.skipUnless(os.environ.get("MTW_POSTGRES_TESTS") == "1", "Opt-in isolated PostgreSQL tests")
class HistoryPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = "mtw_history_" + uuid4().hex
        cls.admin = create_engine(database_url())
        with cls.admin.begin() as c:
            c.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
        cls.engine = create_engine(database_url(), pool_size=8,
            connect_args={"options": f"-csearch_path={cls.schema}"})
        with cls.engine.connect() as c:
            assert c.scalar(text("SELECT current_schema()")) == cls.schema
        cls.sessions = sessionmaker(cls.engine, expire_on_commit=False)
        Base.metadata.create_all(cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        assert cls.schema.startswith("mtw_history_") and len(cls.schema) == 44
        with cls.admin.begin() as c:
            c.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
        cls.admin.dispose()

    def setUp(self):
        with self.sessions.begin() as s:
            s.execute(text("DELETE FROM jobs"))
            s.execute(text("DELETE FROM auth_sessions"))
            s.execute(text("DELETE FROM users"))
        self.temporary = tempfile.TemporaryDirectory(prefix="history-test-", dir=DATA / "reports")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "jobs").mkdir()
        self.enterContext(patch.object(api, "Session", self.sessions))
        self.enterContext(patch.object(auth, "Session", self.sessions))
        self.enterContext(patch.object(api, "job_dir", lambda job_id: self.root / "jobs" / str(job_id)))
        self.enterContext(patch.object(frame_work, "DATA", self.root))
        self.enterContext(patch.dict(api.LIMITS, stored_jobs=100, queued_jobs=10))
        token = secrets.token_urlsafe(32)
        with self.sessions.begin() as s:
            user = User(login="history_fixture", password_hash=auth.hasher.hash(secrets.token_urlsafe(24)))
            s.add(user)
            s.flush()
            self.user_id = user.id
            s.add(AuthSession(token_hash=auth.token_hash(token), user_id=user.id, expires_at=now() + timedelta(hours=1)))
        self.headers = {"Cookie": f"{auth.COOKIE}={token}", "Origin": "http://127.0.0.1:8080", "X-CSRF-Token": auth.csrf_token(token)}

    async def request_async(self, method, path, **kwargs):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://127.0.0.1:8080", headers=self.headers) as c:
            return await c.request(method, path, **kwargs)

    def request(self, method, path, **kwargs):
        return asyncio.run(self.request_async(method, path, **kwargs))

    def upload(self, content=b"admission fixture; not decoded", **parameters):
        return {"files": {"video": ("fixture.mp4", content, "video/mp4")},
                "data": {"parameters": json.dumps({"calibration": CALIBRATION} | parameters)}}

    def seed(self, status="succeeded", **values):
        values.setdefault("owner_id", self.user_id)
        with self.sessions.begin() as s:
            job = Job(status=status, stage=status, parameters={"calibration": CALIBRATION, "frames": 1200},
                      input_ref="test-only", **values)
            s.add(job)
            s.flush()
            return job.id

    def count(self):
        with self.sessions() as s:
            return s.scalar(select(func.count()).select_from(Job))

    def test_boundary_admits_last_then_removes_rejected_upload(self):
        api.LIMITS["stored_jobs"] = 2
        self.seed()
        accepted = self.request("POST", "/api/jobs", **self.upload())
        self.assertEqual(accepted.status_code, 202, accepted.text)
        files_before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        rejected = self.request("POST", "/api/jobs", **self.upload())
        self.assertEqual(rejected.status_code, 429)
        self.assertEqual(rejected.json()["detail"]["code"], "stored_job_limit_reached")
        self.assertEqual(rejected.json()["detail"]["count"], 2)
        self.assertEqual(self.count(), 2)
        self.assertEqual(sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")), files_before)

    def test_two_concurrent_posts_near_limit(self):
        api.LIMITS["stored_jobs"] = 2
        self.seed()
        barrier = Barrier(2)
        arrivals = []

        def synchronize(connection, cursor, statement, parameters, context, executemany):
            if "pg_advisory_xact_lock(7132101)" in statement:
                arrivals.append(1)
                barrier.wait(timeout=10)

        async def pair():
            return await asyncio.gather(*(self.request_async("POST", "/api/jobs", **self.upload()) for _ in range(2)))

        event.listen(self.engine, "before_cursor_execute", synchronize)
        try:
            responses = asyncio.run(pair())
        finally:
            event.remove(self.engine, "before_cursor_execute", synchronize)
        self.assertEqual(len(arrivals), 2)
        self.assertEqual(sorted(r.status_code for r in responses), [202, 429])
        self.assertEqual(self.count(), 2)
        accepted = next(r.json() for r in responses if r.status_code == 202)
        self.assertEqual([p.name for p in (self.root / "jobs").iterdir()], [accepted["id"]])
        print(json.dumps({"concurrent_http": [r.status_code for r in responses], "stored": self.count(),
                          "upload_directories": 1, "both_reached_admission": True}))

    def test_every_state_counts_and_pending_cap_is_separate(self):
        states = ["queued", "dispatching", "running", "succeeded", "failed", "cancelled", "timed_out"]
        for state in states:
            self.seed(status=state)
        api.LIMITS["stored_jobs"] = len(states)
        self.assertEqual(self.request("POST", "/api/jobs", **self.upload()).json()["detail"]["code"],
                         "stored_job_limit_reached")
        api.LIMITS.update(stored_jobs=100, queued_jobs=3)
        response = self.request("POST", "/api/jobs", **self.upload())
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["detail"]["code"], "pending_job_limit_reached")
        self.assertEqual(response.json()["detail"]["count"], 3)
        api.LIMITS["queued_jobs"] = 4
        self.assertEqual(self.request("POST", "/api/jobs", **self.upload()).status_code, 202)
        self.assertEqual(self.count(), 8)

    def test_existing_over_limit_stays_readable_and_cancellable(self):
        success = self.seed(result_ref="result.json")
        queued = self.seed(status="queued")
        self.seed(status="failed")
        api.LIMITS["stored_jobs"] = 1
        directory = api.job_dir(success)
        directory.mkdir()
        fixture = {"artifacts": {}, "fixture": "history access only; no SLAM result claim"}
        (directory / "result.json").write_text(json.dumps(fixture))
        self.assertEqual(self.request("GET", "/api/jobs").json()["total"], 3)
        self.assertEqual(self.request("GET", f"/api/jobs/{success}").status_code, 200)
        self.assertEqual(self.request("GET", f"/api/jobs/{success}/snapshot").status_code, 200)
        self.assertEqual(self.request("GET", f"/api/jobs/{success}/result").json(), fixture)
        self.assertEqual(self.request("POST", f"/api/jobs/{queued}/cancel").json()["status"], "cancelled")
        self.assertEqual(self.request("POST", "/api/jobs", **self.upload()).status_code, 429)
        self.assertEqual(self.count(), 3)
        self.assertTrue((directory / "result.json").exists())

    def test_pagination_ties_has_no_gaps_or_duplicates_and_reads_no_artifacts(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        expected = []
        for i in range(1, 28):
            at = base + timedelta(seconds=int(i > 25))
            job_id = self.seed(id=UUID(int=i), created_at=at)
            expected.append((at, str(job_id)))
        expected = [job_id for _, job_id in sorted(expected, reverse=True)]
        observed = []
        with patch.object(api, "read_json", side_effect=AssertionError("List must not read artifacts")):
            for page in range(1, 5):
                response = self.request("GET", f"/api/jobs?page={page}&page_size=7")
                self.assertEqual(response.status_code, 200, response.text)
                data = response.json()
                self.assertEqual((data["total"], data["total_pages"], data["page"], data["page_size"]), (27, 4, page, 7))
                self.assertEqual(data["has_next"], page < 4)
                self.assertEqual(data["has_previous"], page > 1)
                self.assertEqual(data["stored_job_limit"], 100)
                observed.extend(row["id"] for row in data["items"])
                for row in data["items"]:
                    self.assertFalse(set(row) & {"points", "poses", "trajectory", "Twc", "jsonl"})
        self.assertEqual(observed, expected)
        self.assertEqual(len(set(observed)), 27)
        default = self.request("GET", "/api/jobs").json()
        self.assertEqual((len(default["items"]), default["page_size"]), (20, 20))
        self.assertEqual(len(self.request("GET", "/api/jobs?page_size=100").json()["items"]), 27)
        self.assertEqual(self.request("GET", "/api/jobs?page=2147483647").json()["items"], [])
        print(json.dumps({"pagination_total": 27, "pages": 4, "page_size": 7, "equal_timestamps": 25,
                          "unique_ordered_ids": len(set(observed))}))

    def test_empty_and_invalid_pagination(self):
        data = self.request("GET", "/api/jobs").json()
        self.assertEqual((data["items"], data["total"], data["total_pages"], data["has_next"], data["has_previous"]),
                         ([], 0, 0, False, False))
        for query in ["page=0", "page=-1", "page=x", "page=2147483648", "page=1.5", "page_size=0",
                      "page_size=-1", "page_size=101", "page_size=x", "page_size=1.5", "limit=20",
                      "unknown=1", "page=1&page=2", "page_size=2&page_size=2"]:
            with self.subTest(query=query):
                self.assertEqual(self.request("GET", "/api/jobs?" + query).status_code, 422)
        self.assertEqual(self.count(), 0)

    def test_partially_written_upload_is_removed(self):
        writes = []
        original = api.run_in_threadpool

        async def record(function, *args, **kwargs):
            value = await original(function, *args, **kwargs)
            if getattr(function, "__name__", None) == "write":
                writes.append(value)
            return value

        with patch.object(api, "run_in_threadpool", record):
            response = self.request("POST", "/api/jobs", **self.upload(b"x" * (1048576 + 2), file_bytes=1048577))
        self.assertEqual(response.status_code, 413)
        self.assertEqual(writes, [1048576])
        self.assertEqual(list((self.root / "jobs").iterdir()), [])
        self.assertEqual(self.request("POST", "/api/jobs", **self.upload(b"")).status_code, 422)
        self.assertEqual(self.request("POST", "/api/jobs", files={"video": ("x", b"x")},
                                      data={"parameters": "{}"}).status_code, 422)
        self.assertEqual(self.count(), 0)
        self.assertEqual(list((self.root / "jobs").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
