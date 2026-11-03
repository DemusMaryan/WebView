"""Authentication/access tests: real isolated PostgreSQL, no dispatcher or SLAM."""
import asyncio
from datetime import timedelta
import importlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from PIL import Image
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from mtw import api, auth, frame_work, service_auth
from mtw.config import CONFIG, CONFIG_PATH, DATA, REPO, database_url
from mtw.contracts import empty_snapshot
from mtw.db import AuthSession, Base, Job, User, now
from test_contracts import CALIBRATION


@unittest.skipUnless(os.environ.get("MTW_POSTGRES_TESTS") == "1", "Opt-in isolated PostgreSQL tests")
class AuthenticationPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = "mtw_auth_" + uuid4().hex
        cls.admin = create_engine(database_url())
        with cls.admin.begin() as c:
            c.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
        cls.engine = create_engine(database_url(), connect_args={"options": f"-csearch_path={cls.schema}"})
        with cls.engine.connect() as c:
            assert c.scalar(text("SELECT current_schema()")) == cls.schema
        Base.metadata.create_all(cls.engine)
        cls.sessions = sessionmaker(cls.engine, expire_on_commit=False)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        assert cls.schema.startswith("mtw_auth_") and len(cls.schema) == 41
        with cls.admin.begin() as c:
            c.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
        cls.admin.dispose()

    def setUp(self):
        with self.sessions.begin() as s:
            for table in ("jobs", "auth_sessions", "users"):
                s.execute(text(f"DELETE FROM {table}"))
        self.temporary = tempfile.TemporaryDirectory(prefix="auth-test-", dir=DATA / "reports")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "jobs").mkdir()
        self.enterContext(patch.object(api, "Session", self.sessions))
        self.enterContext(patch.object(auth, "Session", self.sessions))
        self.enterContext(patch.object(api, "job_dir", lambda value: self.root / "jobs" / str(value)))
        self.enterContext(patch.object(frame_work, "DATA", self.root))
        self.enterContext(patch.dict(api.LIMITS, stored_jobs=100, queued_jobs=10))
        self.password = secrets.token_urlsafe(24)  # Never persisted or printed.

    def client(self, **kwargs):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),
            base_url="https://127.0.0.1:8443", headers={"Origin": "https://127.0.0.1:8443"}, **kwargs)

    async def bootstrap(self, client):
        response = await client.get("/api/auth/session")
        self.assertEqual(response.status_code, 200)
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        return response

    async def register(self, client, login):
        await self.bootstrap(client)
        response = await client.post("/api/auth/register", json={"login": login, "password": self.password})
        self.assertEqual(response.status_code, 201)
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        return response.json()["user"]

    def seed(self, owner_id=None, status="succeeded", artifacts=False, **values):
        from uuid import UUID
        with self.sessions.begin() as s:
            job = Job(owner_id=UUID(owner_id) if owner_id else None, status=status, stage=status,
                      input_ref="input.video", parameters={"frames": 1200, "calibration": CALIBRATION},
                      result_ref="result.json" if artifacts else None, **values)
            s.add(job)
            s.flush()
            job_id = str(job.id)
        if artifacts:
            directory = api.job_dir(job_id)
            (directory / "previews").mkdir(parents=True)
            (directory / "native").mkdir()
            Image.new("RGB", (4, 4), (0, 64, 128)).save(directory / "previews/000000.png")
            (directory / "input.video").write_bytes(b"authorization fixture, not decoded")
            (directory / "result.json").write_text(json.dumps({"artifacts": {"trajectory.json": "unused"}}))
            (directory / "native/trajectory.json").write_text('{"fixture":"access control only"}')
            (directory / "latest.json").write_text(json.dumps(empty_snapshot() | {"job_id": job_id, "stage": status}))
        return job_id

    def test_registration_password_session_rotation_logout_and_expiry(self):
        async def run():
            async with self.client() as client:
                anon = await self.bootstrap(client)
                old_cookie = client.cookies.get(auth.COOKIE)
                self.assertTrue("httponly" in anon.headers["set-cookie"].lower())
                self.assertTrue("samesite=strict" in anon.headers["set-cookie"].lower())
                self.assertTrue("; secure" in anon.headers["set-cookie"].lower())
                user = await self.register(client, "Account_A")
                self.assertEqual(user["login"], "account_a")
                token = client.cookies.get(auth.COOKIE)
                self.assertTrue(token != old_cookie)
                self.assertEqual((await client.get("/api/auth/me")).json()["id"], user["id"])
                with self.sessions() as s:
                    stored = s.get(User, user["id"])
                    self.assertTrue(stored.password_hash.startswith("$argon2id$"))
                    self.assertTrue(auth.hasher.verify(stored.password_hash, self.password))
                    self.assertTrue(self.password not in stored.password_hash)
                    self.assertEqual(len(s.get(AuthSession, auth.token_hash(token)).token_hash), 64)
                    self.assertIsNotNone(s.get(AuthSession, auth.token_hash(old_cookie)).revoked_at)
                duplicate = await client.post("/api/auth/register", json={"login": "ACCOUNT_A", "password": self.password})
                self.assertEqual(duplicate.status_code, 409)
                self.assertEqual((await client.post("/api/auth/logout")).status_code, 204)
                self.assertEqual((await client.get("/api/jobs")).status_code, 401)
                self.assertEqual((await client.get("/api/jobs", headers={"Cookie": f"{auth.COOKIE}={token}"})).status_code, 401)
                await self.bootstrap(client)
                wrong = await client.post("/api/auth/login", json={"login": "account_a", "password": secrets.token_urlsafe(24)})
                self.assertEqual(wrong.status_code, 401)
                correct = await client.post("/api/auth/login", json={"login": "account_a", "password": self.password})
                self.assertEqual(correct.status_code, 200)
                client.headers["X-CSRF-Token"] = correct.json()["csrf_token"]
                token = client.cookies.get(auth.COOKIE)
                with self.sessions.begin() as s:
                    s.get(AuthSession, auth.token_hash(token)).expires_at = now() - timedelta(seconds=1)
                self.assertEqual((await client.get("/api/auth/me")).status_code, 401)
                self.assertEqual((await client.get("/api/jobs")).status_code, 401)
        asyncio.run(run())

    def test_two_users_pagination_every_job_route_and_legacy_hidden(self):
        async def run():
            async with self.client() as a, self.client() as b, self.client() as anon:
                ua, ub = await self.register(a, "owner_a"), await self.register(b, "owner_b")
                tie = now()
                ids_a = [self.seed(ua["id"], created_at=tie) for _ in range(25)]
                ids_b = [self.seed(ub["id"], created_at=tie) for _ in range(25)]
                legacy = self.seed()
                for client, ids in ((a, ids_a), (b, ids_b)):
                    observed = []
                    for page in range(1, 5):
                        response = await client.get(f"/api/jobs?page={page}&page_size=7")
                        self.assertEqual(response.status_code, 200)
                        value = response.json()
                        self.assertEqual((value["total"], value["total_pages"]), (25, 4))
                        observed.extend(row["id"] for row in value["items"])
                    self.assertEqual(observed, sorted(ids, reverse=True))
                task = self.seed(ua["id"], artifacts=True)
                paths = ["", "/snapshot", "/frames/0", "/video", "/result", "/artifacts/trajectory.json"]
                for suffix in paths:
                    route = f"/api/jobs/{task}{suffix}"
                    self.assertEqual((await a.get(route)).status_code, 200, suffix)
                    self.assertEqual((await b.get(route)).status_code, 404, suffix)
                    self.assertEqual((await anon.get(route)).status_code, 401, suffix)
                self.assertEqual((await a.get(f"/api/jobs/{legacy}")).status_code, 404)
                self.assertEqual((await b.post(f"/api/jobs/{task}/cancel")).status_code, 404)
                self.assertEqual((await anon.post(f"/api/jobs/{task}/cancel")).status_code, 401)
                self.assertEqual((await a.post(f"/api/jobs/{task}/cancel")).status_code, 202)
                self.assertEqual((await a.get(f"/api/jobs/{task}/frames/0")).headers["cache-control"], "private, no-store")
        asyncio.run(run())

    def test_owner_spoofing_global_stored_and_pending_caps(self):
        async def run():
            async with self.client() as a, self.client() as b:
                ua, ub = await self.register(a, "quota_a"), await self.register(b, "quota_b")
                upload = {"files": {"video": ("fixture.mp4", b"not decoded")},
                          "data": {"parameters": json.dumps({"calibration": CALIBRATION})}}
                spoof = upload | {"data": upload["data"] | {"owner_id": ub["id"]}}
                self.assertEqual((await a.post("/api/jobs", **spoof)).status_code, 422)
                spoof = upload | {"data": {"parameters": json.dumps({"calibration": CALIBRATION, "owner_id": ub["id"]})}}
                self.assertEqual((await a.post("/api/jobs", **spoof)).status_code, 422)
                valid = await a.post("/api/jobs", **upload)
                self.assertEqual(valid.status_code, 202)
                self.assertEqual(valid.json()["owner_id"], ua["id"])
                self.seed(ub["id"])
                self.seed()
                before = sorted(p.name for p in (self.root / "jobs").iterdir())
                api.LIMITS["stored_jobs"] = 3
                full = await b.post("/api/jobs", **upload)
                self.assertEqual(full.status_code, 429)
                self.assertEqual(full.json()["detail"]["code"], "stored_job_limit_reached")
                self.assertEqual(full.json()["detail"]["count"], 3)
                self.assertEqual((await b.get("/api/jobs")).json()["total"], 1)
                api.LIMITS.update(stored_jobs=100, queued_jobs=1)
                pending = await b.post("/api/jobs", **upload)
                self.assertEqual(pending.status_code, 429)
                self.assertEqual(pending.json()["detail"]["code"], "pending_job_limit_reached")
                self.assertEqual(sorted(p.name for p in (self.root / "jobs").iterdir()), before)
        asyncio.run(run())

    def test_csrf_including_login_and_sanitized_validation(self):
        async def run():
            async with self.client() as client:
                payload = {"login": "csrf_a", "password": self.password}
                self.assertEqual((await client.post("/api/auth/register", json=payload)).status_code, 403)
                await self.bootstrap(client)
                for headers in ({"X-CSRF-Token": ""}, {"X-CSRF-Token": "invalid"}, {"Origin": "http://localhost:9999"},
                                {"X-CSRF-Token": "\u00e9"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}):
                    if headers.get("X-CSRF-Token") == "\u00e9":
                        headers = {b"X-CSRF-Token": b"\xe9"}
                    self.assertEqual((await client.post("/api/auth/register", json=payload, headers=headers)).status_code, 403)
                client.headers.pop("Origin")
                self.assertEqual((await client.post("/api/auth/register", json=payload)).status_code, 403)
                client.headers["Origin"] = "https://127.0.0.1:8443"
                invalid = await client.post("/api/auth/register", json=payload | {"owner_id": "forbidden"})
                self.assertEqual(invalid.status_code, 422)
                self.assertTrue(self.password not in invalid.text)
                self.assertTrue('"input"' not in invalid.text)
                await self.register(client, "csrf_a")
                self.assertEqual((await client.post("/api/auth/logout", headers={"X-CSRF-Token": ""})).status_code, 403)
                self.assertEqual((await client.get("/api/auth/me")).status_code, 200)
        asyncio.run(run())

    def cli_config(self, schema=None):
        root = self.root / (schema or "cli")
        root.mkdir()
        url = make_url(database_url()).update_query_dict({"options": f"-csearch_path={schema or self.schema}"})
        (root / "database-url").write_text(url.render_as_string(hide_password=False))
        (root / "database-url").chmod(0o600)
        config = CONFIG_PATH.read_text().replace(f'data_root = "{CONFIG["paths"]["data_root"]}"', f'data_root = "{root}"')
        path = root / "config.toml"
        path.write_text(config)
        return os.environ.copy() | {"MTW_CONFIG": str(path), "PYTHONPATH": str(REPO / "backend")}

    def test_explicit_cli_assigns_only_unowned_in_isolated_schema(self):
        async def setup():
            async with self.client() as a, self.client() as b:
                return await self.register(a, "cli_a"), await self.register(b, "cli_b")
        ua, ub = asyncio.run(setup())
        old = [self.seed(), self.seed()]
        owned = self.seed(ub["id"])
        env = self.cli_config()
        def cli(*args):
            return subprocess.run(["bash", str(REPO / "scripts/assign-unowned"), *args], env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(cli("--all-unowned").returncode, 2)
        self.assertEqual(cli("--login", "missing", "--all-unowned").returncode, 2)
        dry = cli("--login", "cli_a", "--all-unowned", "--dry-run")
        self.assertEqual(dry.returncode, 0)
        self.assertEqual(json.loads(dry.stdout)["assigned"], 0)
        with self.sessions() as s:
            self.assertTrue(all(s.get(Job, job_id).owner_id is None for job_id in old))
        assigned = cli("--login", "cli_a", "--all-unowned")
        self.assertEqual(assigned.returncode, 0)
        self.assertEqual(json.loads(assigned.stdout)["assigned"], 2)
        with self.sessions() as s:
            self.assertTrue(all(str(s.get(Job, job_id).owner_id) == ua["id"] for job_id in old))
            self.assertEqual(str(s.get(Job, owned).owner_id), ub["id"])

    def test_https_mode_always_secure_and_rejects_http_bootstrap(self):
        auth.validate_settings(auth.SETTINGS)
        with self.assertRaises(ValueError):
            auth.validate_settings(auth.SETTINGS | {"cookie_mode": "https", "allowed_origins": ["http://127.0.0.1:8080"]})
        with patch.dict(auth.SETTINGS, cookie_mode="https", allowed_origins=["https://127.0.0.1:8443"]):
            async def run():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="https://127.0.0.1:8443") as c:
                    response = await c.get("/api/auth/session")
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue("; secure" in response.headers["set-cookie"].lower())
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://127.0.0.1:8000") as c:
                    self.assertEqual((await c.get("/api/auth/session")).status_code, 400)
            asyncio.run(run())

    def test_service_cookie_cannot_replace_private_key_and_dispatcher_sends_key(self):
        os.environ.setdefault("MTW_SERVER_ID", "compute-1")
        compute = importlib.import_module("mtw.compute")
        from mtw import dispatcher
        key = secrets.token_hex(32)
        reservation = {"job_id": str(uuid4()), "dispatch_token": str(uuid4())}
        async def run():
            async with self.client() as browser:
                await self.register(browser, "service_browser")
                cookie = browser.cookies.get(auth.COOKIE)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=compute.app), base_url="http://127.0.0.1:8101") as c:
                self.assertEqual((await c.get("/health")).status_code, 401)
                self.assertEqual((await c.get("/health", headers={"Cookie": f"{auth.COOKIE}={cookie}"})).status_code, 401)
                self.assertEqual((await c.post("/execute", json=reservation)).status_code, 401)
                self.assertEqual((await c.post("/execute", json=reservation, headers={"X-MTW-Service-Key": "wrong"})).status_code, 401)
                self.assertEqual((await c.get("/health", headers={b"X-MTW-Service-Key": b"\xe9"})).status_code, 401)
                self.assertEqual((await c.get("/health", headers={"X-MTW-Service-Key": key})).status_code, 200)
                self.assertEqual((await c.post("/execute", json=reservation, headers={"X-MTW-Service-Key": key})).status_code, 409)
            def check(request):
                self.assertTrue(request.headers.get("X-MTW-Service-Key") == key)
                self.assertEqual(request.url.port, 8081)
                return httpx.Response(202, json={"status": "accepted"})
            async with httpx.AsyncClient(transport=httpx.MockTransport(check)) as c:
                with patch.object(dispatcher, "still_reserved", side_effect=[True, False, False]):
                    await dispatcher.deliver(c, reservation, asyncio.Event())
        with patch.object(service_auth, "service_key", return_value=key), patch.object(compute, "Session", self.sessions), \
                patch.object(compute, "run_job", side_effect=AssertionError("No worker may start")):
            asyncio.run(run())

    def test_owned_reservation_service_accepts_without_browser_cookie_once(self):
        os.environ.setdefault("MTW_SERVER_ID", "compute-1")
        compute = importlib.import_module("mtw.compute")
        key = secrets.token_hex(32)
        async def run():
            async with self.client() as browser:
                user = await self.register(browser, "handoff_owner")
            reservation_token = uuid4()
            with self.sessions.begin() as s:
                from uuid import UUID
                job = Job(owner_id=UUID(user["id"]), status="dispatching", stage="dispatching",
                          dispatch_token=reservation_token, parameters={"execution_seconds": 180}, input_ref="fixture")
                s.add(job); s.flush(); job_id = job.id
            finished = asyncio.Event()
            calls = []
            async def no_native_task(value, budget):
                calls.append((value, budget))
                await finished.wait()
                with self.sessions.begin() as s:
                    s.get(Job, value).status = "failed"  # Test-only completion, no CV claim.
                compute.active = None
                compute.slot.release()
            with patch.object(compute, "run_job", no_native_task), patch.object(compute, "slot", asyncio.Lock()), \
                    patch.object(compute, "active", None), patch.object(compute, "manager_task", None):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=compute.app), base_url="http://127.0.0.1:8101") as c:
                    reservation = {"job_id": str(job_id), "dispatch_token": str(reservation_token)}
                    try:
                        accepted = await c.post("/execute", json=reservation, headers={"X-MTW-Service-Key": key})
                        self.assertEqual(accepted.status_code, 202)
                        self.assertEqual(accepted.json()["server_id"], "compute-1")
                        duplicate = await c.post("/execute", json=reservation, headers={"X-MTW-Service-Key": key})
                        self.assertEqual(duplicate.status_code, 202)
                        self.assertTrue(duplicate.json()["duplicate"])
                    finally:
                        finished.set()
                        if compute.manager_task:
                            await compute.manager_task
                    self.assertEqual(calls, [(job_id, 180)])
                    self.assertFalse(compute.slot.locked())
        with patch.object(service_auth, "service_key", return_value=key), patch.object(compute, "Session", self.sessions):
            asyncio.run(run())

    def test_private_service_key_creation_and_permission_guard(self):
        path = self.root / "service-key"
        service_auth.service_key.cache_clear()
        try:
            with patch.object(service_auth, "KEY_FILE", path):
                service_auth.ensure_service_key()
                first = service_auth.service_key()
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                service_auth.ensure_service_key()
                self.assertTrue(first == service_auth.service_key())
                path.chmod(0o644)
                service_auth.service_key.cache_clear()
                with self.assertRaises(RuntimeError):
                    service_auth.service_key()
        finally:
            service_auth.service_key.cache_clear()

    def test_status_reports_missing_service_credential_without_crashing(self):
        from contextlib import redirect_stdout
        import io
        from mtw import control
        output = io.StringIO()
        with patch.object(control, "pg_running", return_value=True), patch.object(control, "owned", return_value=None), \
                patch.object(control, "service_headers", side_effect=RuntimeError("unavailable")), \
                patch.object(control.httpx, "get") as get, redirect_stdout(output):
            get.return_value.json.return_value = {"status": "ok"}
            control.status()
        value = json.loads(output.getvalue())
        self.assertIn("health_error", value["services"]["compute-1"])
        self.assertIn("health_error", value["services"]["compute-2"])

    def test_migration_preserves_legacy_rows(self):
        schema = "mtw_migration_" + uuid4().hex
        with self.admin.begin() as c:
            c.execute(text(f'CREATE SCHEMA "{schema}"'))
        migration_engine = create_engine(database_url(), connect_args={"options": f"-csearch_path={schema}"})
        try:
            env = self.cli_config(schema)
            def migrate(revision):
                r = subprocess.run([sys.executable, "-m", "alembic", "-c", str(REPO / "backend/alembic.ini"),
                                    "upgrade", revision], env=env, capture_output=True, text=True, timeout=20)
                self.assertEqual(r.returncode, 0)
            migrate("0002_active_server")
            sessions = sessionmaker(migration_engine, expire_on_commit=False)
            with sessions.begin() as s:
                s.add(Job(parameters={}, input_ref="legacy", status="succeeded"))
            with migration_engine.connect() as c:
                before = c.scalar(text("SELECT row_to_json(jobs) FROM jobs"))
            migrate("head")
            with migration_engine.connect() as c:
                self.assertEqual(c.scalar(text("SELECT row_to_json(jobs) FROM jobs")), before)
                self.assertEqual(c.scalar(text("SELECT count(*) FROM users")), 0)
                self.assertEqual(c.scalar(text("SELECT count(*) FROM auth_sessions")), 0)
                self.assertEqual(c.scalar(text("SELECT version_num FROM alembic_version")), "0003_authentication")
        finally:
            migration_engine.dispose()
            assert schema.startswith("mtw_migration_") and len(schema) == 46
            with self.admin.begin() as c:
                c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))

    def test_http_and_spoofed_forwarding_rejected_trusted_proxy_accepted(self):
        from mtw import proxy
        key = secrets.token_hex(32)
        async def run():
            async with self.client() as browser:
                user = await self.register(browser, "tls_owner")
                cookie = browser.cookies.get(auth.COOKIE)
                headers = {"Origin": "https://127.0.0.1:8443", "Cookie": f"{auth.COOKIE}={cookie}",
                           "Host": "127.0.0.1:8443", "X-Forwarded-Host": "127.0.0.1:8443", "X-Forwarded-Proto": "https"}
                for peer in ("127.0.0.1", "192.0.2.10"):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app, client=(peer, 12345)),
                                                 base_url="http://127.0.0.1:8000") as client:
                        self.assertEqual((await client.get("/api/auth/me", headers=headers)).status_code, 400)
                        self.assertEqual((await client.get("/api/auth/me", headers=headers | {"X-MTW-Proxy-Key": "wrong"})).status_code, 400)
                        accepted = await client.get("/api/auth/me", headers=headers | {"X-MTW-Proxy-Key": key})
                        self.assertEqual(accepted.status_code, 200 if peer == "127.0.0.1" else 400)
                        if peer == "127.0.0.1":
                            self.assertEqual(accepted.json()["id"], user["id"])
                self.assertEqual((await browser.get("/api/auth/me")).status_code, 200)
        with patch.object(proxy, "proxy_key", return_value=key):
            asyncio.run(run())

    def test_existing_cookie_upgraded_to_secure_without_extending_session(self):
        async def run():
            async with self.client() as client:
                await self.register(client, "upgrade_cookie")
                token = client.cookies.get(auth.COOKIE)
                with self.sessions() as s:
                    expires = s.get(AuthSession, auth.token_hash(token)).expires_at
                client.cookies.clear()
                client.cookies.set(auth.COOKIE, token, domain="127.0.0.1", path="/")
                response = await client.get("/api/auth/session")
                self.assertEqual(response.status_code, 200)
                self.assertTrue("; secure" in response.headers["set-cookie"].lower())
                self.assertTrue(client.cookies.get(auth.COOKIE) == token)
                with self.sessions() as s:
                    self.assertEqual(s.get(AuthSession, auth.token_hash(token)).expires_at, expires)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
