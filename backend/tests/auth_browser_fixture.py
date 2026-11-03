"""Disposable auth browser environment: isolated schema, API/Nginx, no workers.

Copies/references existing verified result artifacts solely as access fixtures.
Test passwords are derived in memory from a private disposable fixture seed.
"""
import argparse
from datetime import timedelta
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from uuid import uuid4
import httpx
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from mtw.auth import hasher
from mtw.capacity import ensure_space
from mtw.config import CONFIG, CONFIG_PATH, DATA, REPO, database_url, job_dir
from mtw.db import Job, Session, User, now
from mtw.files import atomic_json, read_json
from mtw.processes import identity


def prepare():
    ensure_space()
    for port in (8180, 8200):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))  # Fail without touching any existing listener.
    suffix = uuid4().hex
    root = DATA / "reports" / ("auth-browser-" + suffix)
    root.mkdir(mode=0o700)
    schema = "mtw_browser_" + suffix
    control = {"root": str(root), "schema": schema, "processes": [], "base": "http://127.0.0.1:8180"}
    atomic_json(root / "control.json", control)
    try:
        return prepare_environment(root, schema, control)
    except BaseException:
        cleanup(root)
        raise


def prepare_environment(root, schema, control):
    admin = create_engine(database_url())
    with admin.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()
    url = make_url(database_url()).update_query_dict({"options": f"-csearch_path={schema}"})
    (root / "database-url").write_text(url.render_as_string(hide_password=False))
    (root / "database-url").chmod(0o600)
    config = CONFIG_PATH.read_text().replace(f'data_root = "{CONFIG["paths"]["data_root"]}"', f'data_root = "{root}"')
    config = config.replace('api = 8000', 'api = 8200').replace('entry = 8080', 'entry = 8180')
    config = config.replace('"http://127.0.0.1:8080", "http://localhost:8080"', '"http://127.0.0.1:8180", "http://localhost:8180"')
    (root / "config.toml").write_text(config)
    env = os.environ | {"MTW_CONFIG": str(root / "config.toml"), "PYTHONPATH": str(REPO / "backend")}
    subprocess.run([sys.executable, "-m", "alembic", "-c", str(REPO / "backend/alembic.ini"), "upgrade", "head"],
                   env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    engine = create_engine(url)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with engine.connect() as c:
        assert c.scalar(text("SELECT current_schema()")) == schema
    credentials = {"base": control["base"], "users": {}}
    fixture_seed = secrets.token_bytes(32)
    (root / "fixture-seed").write_bytes(fixture_seed)
    (root / "fixture-seed").chmod(0o600)
    for label, source_id in (("a", "ad174e33-fa00-467e-9909-e580bbac0e6f"),
                             ("b", "e8688279-ad66-42af-b7ec-3ce672547406")):
        login = f"browser_{label}"
        password = hmac.new(fixture_seed, login.encode(), hashlib.sha256).hexdigest()
        with Session() as public:
            original = public.get(Job, source_id)
            assert original.status == "succeeded"
            parameters = original.parameters
        source = job_dir(source_id)
        job_id = uuid4()
        directory = root / "jobs" / str(job_id)
        (directory / "native").mkdir(parents=True)
        (directory / "previews").mkdir()
        manifest = read_json(source / "result.json") | {"job_id": str(job_id)}
        latest = read_json(source / "latest.json") | {"job_id": str(job_id)}
        latest["frame_url"] = f"/api/jobs/{job_id}/frames/{latest['frame_id']}"
        atomic_json(directory / "result.json", manifest)
        atomic_json(directory / "latest.json", latest)
        (directory / "frame-work-policy.json").write_text('{"version":1,"cleanup_decoded_frames":true}')
        os.link(source / "input.video", directory / "input.video")
        preview_name = f"{latest['frame_id']:06d}.png"
        preview = source / "previews" / preview_name
        if not preview.is_file():
            preview = source / "sequence/rgb" / preview_name
        os.link(preview, directory / "previews" / preview_name)
        for name in ("trajectory.json", "map_points.json", "native-summary.json", "snapshots.jsonl"):
            os.link(source / "native" / name, directory / "native" / name)
        with sessions.begin() as s:
            user = User(login=login, password_hash=hasher.hash(password))
            s.add(user)
            s.flush()
            s.add(Job(id=job_id, owner_id=user.id, status="succeeded", stage="succeeded", parameters=parameters,
                      input_ref="input.video", result_ref="result.json", snapshot_ref="latest.json",
                      server_id=manifest["server_id"], processed_frames=manifest["processed_frames"],
                      total_frames=manifest["processed_frames"]))
            if label == "a":
                for _ in range(20):
                    s.add(Job(owner_id=user.id, status="failed", stage="failed", parameters=parameters,
                              input_ref="auth history fixture only", created_at=now() - timedelta(days=1)))
        credentials["users"][label] = {"login": user.login, "job_id": str(job_id),
                                       "source_job_id": source_id, "frames": manifest["processed_frames"]}
    with sessions.begin() as s:
        legacy = Job(status="failed", stage="failed", parameters={}, input_ref="unowned fixture")
        s.add(legacy)
        s.flush()
        credentials["legacy_job_id"] = str(legacy.id)
    engine.dispose()
    (root / "credentials.json").write_text(json.dumps(credentials))
    (root / "credentials.json").chmod(0o600)
    nginx = root / "nginx"
    nginx.mkdir()
    (nginx / "nginx.conf").write_text(f'''pid "{nginx}/nginx.pid";
error_log "{root}/nginx-error.log";
events {{ worker_connections 64; }}
http {{
 include /etc/nginx/mime.types;
 access_log "{root}/nginx-access.log";
 server {{ listen 127.0.0.1:8180;
  location /api/ {{ proxy_pass http://127.0.0.1:8200; }}
  location / {{ root "{DATA}/ui"; try_files $uri $uri/ /index.html; add_header Cache-Control "no-store"; }}
 }}
}}
''')
    commands = [([sys.executable, "-m", "uvicorn", "mtw.api:app", "--host", "127.0.0.1", "--port", "8200", "--no-access-log", "--no-proxy-headers"], "8200"),
                (["nginx", "-p", str(nginx) + "/", "-c", "nginx.conf", "-g", "daemon off;"], str(nginx))]
    for argv, marker in commands:
        with (root / ("api.log" if marker == "8200" else "nginx.log")).open("wb") as log:
            child = subprocess.Popen(argv, env=env, cwd=REPO, stdout=log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
        item = identity(child.pid)
        deadline = time.monotonic() + 2
        while item is None and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
            item = identity(child.pid)
        if item is None:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
            raise RuntimeError("Fixture process failed before PID ownership could be recorded")
        control["processes"].append({"pid": child.pid, "start_ticks": item["start_ticks"], "marker": marker})
        atomic_json(root / "control.json", control)
    for _ in range(100):
        try:
            r = httpx.get(control["base"] + "/api/auth/session", timeout=1, trust_env=False)
            if r.status_code == 200:
                print(json.dumps({"root": str(root), "schema": schema, "base": control["base"], "jobs": 23}))
                return
        except httpx.HTTPError:
            pass
        time.sleep(.1)
    raise RuntimeError("Isolated browser environment failed to start; inspect private fixture logs")


def cleanup(root):
    root = Path(root).resolve()
    assert root.parent == (DATA / "reports").resolve() and root.name.startswith("auth-browser-")
    control = read_json(root / "control.json")
    assert control["root"] == str(root)
    schema = control["schema"]
    assert schema == "mtw_browser_" + root.name.removeprefix("auth-browser-") and len(schema) == 44
    for process in reversed(control["processes"]):
        item = identity(process["pid"])
        if item is None:
            continue
        assert item["start_ticks"] == process["start_ticks"] and process["marker"] in item["argv"]
        os.kill(process["pid"], signal.SIGTERM)
        for _ in range(100):
            if identity(process["pid"]) is None:
                break
            time.sleep(.1)
        assert identity(process["pid"]) is None, "Fixture process must stop before schema/file cleanup"
    engine = create_engine(database_url())
    with engine.begin() as c:
        c.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    engine.dispose()
    shutil.rmtree(root)  # Only this validated native test environment.
    print(json.dumps({"fixture_removed": True, "schema_removed": schema, "workers_started": 0}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "cleanup"])
    parser.add_argument("--root")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare()
    elif args.root:
        cleanup(args.root)
    else:
        parser.error("cleanup requires the explicit fixture --root")
