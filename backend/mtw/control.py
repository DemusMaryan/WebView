"""Project-scoped native setup/start/status/stop. Never manage system services."""
import argparse
import asyncio
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
import httpx
from .config import CONFIG_PATH, DATA, LIMITS, PORTS, REPO
from .files import atomic_json, read_json
from .processes import identity

PG_BIN = Path("/usr/lib/postgresql/16/bin")
PGDATA = DATA / "pgdata"
SOCKET = DATA / "pgsocket"
NAMES = ("api", "compute-1", "compute-2", "nginx", "dispatcher")


def command(argv, **kwargs):
    return subprocess.run(list(map(str, argv)), check=True, **kwargs)


@contextmanager
def control_lock():
    with (DATA / "control.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def owned(name):
    record = read_json(DATA / "pids" / f"{name}.json")
    if not record:
        return None
    current = identity(record["pid"])
    if current is None:
        return None
    if current["start_ticks"] != record["start_ticks"]:
        return None  # A stale record must never signal an unrelated reused PID.
    if record["marker"] not in current["argv"]:
        raise RuntimeError(f"PID ownership mismatch for {name}; refusing to manage it")
    return current


def pg_running():
    pidfile = PGDATA / "postmaster.pid"
    if not pidfile.exists():
        return False
    item = identity(int(pidfile.read_text().splitlines()[0]))
    if item is None:
        return False
    if str(PGDATA) not in item["argv"] or "postgres" not in item["argv"]:
        raise RuntimeError("PostgreSQL PID is not this project's cluster")
    return True


def pg_start():
    if not (DATA / "cluster-owner.json").exists():
        raise RuntimeError("Run scripts/setup first; no project cluster ownership marker")
    if not pg_running():
        SOCKET.mkdir(exist_ok=True)
        SOCKET.chmod(0o700)
        command([PG_BIN / "pg_ctl", "-D", PGDATA, "-l", DATA / "logs/postgres.log", "-o",
                 f"-h 127.0.0.1 -p {PORTS['postgres']} -k {SOCKET}", "-w", "start"], stdout=subprocess.DEVNULL)


def setup():
    import psycopg
    from psycopg import sql
    from sqlalchemy.engine import URL
    if not (PGDATA / "PG_VERSION").exists():
        if PGDATA.exists() and any(PGDATA.iterdir()):
            raise RuntimeError("Refusing to initialize a nonempty PostgreSQL directory")
        password = secrets.token_hex(24)
        password_file = DATA / "admin-password"
        password_file.write_text(password, encoding="utf-8")
        password_file.chmod(0o600)
        command([PG_BIN / "initdb", "-D", PGDATA, "--username=mtw_admin", "--auth-local=trust",
                 "--auth-host=scram-sha-256", f"--pwfile={password_file}", "--encoding=UTF8", "--no-locale"],
                 stdout=subprocess.DEVNULL)
        atomic_json(DATA / "cluster-owner.json", {"purpose": "MonoTrajectoryWeb only", "pgdata": str(PGDATA)})
    if read_json(DATA / "cluster-owner.json", {}).get("pgdata") != str(PGDATA):
        raise RuntimeError("Existing cluster has no matching project ownership marker")
    pg_start()
    if not (DATA / "database-url").exists():
        password = secrets.token_hex(24)
        with psycopg.connect(host="127.0.0.1", port=PORTS["postgres"], dbname="postgres", user="mtw_admin",
                password=(DATA / "admin-password").read_text(), autocommit=True) as connection:
            # This isolated cluster has a separate application role and database.
            connection.execute(sql.SQL("CREATE ROLE mtw LOGIN PASSWORD {}").format(sql.Literal(password)))
            connection.execute("CREATE DATABASE mtw OWNER mtw")
        url = URL.create("postgresql+psycopg", username="mtw", password=password,
                         host="127.0.0.1", port=PORTS["postgres"], database="mtw")
        (DATA / "database-url").write_text(url.render_as_string(hide_password=False), encoding="utf-8")
        (DATA / "database-url").chmod(0o600)
    command([sys.executable, "-m", "alembic", "-c", REPO / "backend/alembic.ini", "upgrade", "head"])
    write_nginx()
    print(json.dumps({"setup": "ready", "data_root": str(DATA), "postgres_port": PORTS["postgres"]}))


def write_nginx():
    prefix = DATA / "nginx"
    prefix.mkdir(exist_ok=True)
    for name in ("client_temp", "proxy_temp"):
        (prefix / name).mkdir(exist_ok=True)
    # Config is private to the project; no /etc/nginx or system service changes.
    config = f'''worker_processes 1;
pid "{prefix}/nginx.pid";
error_log "{DATA}/logs/nginx-error.log";
events {{ worker_connections 128; }}
http {{
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    client_body_temp_path "{prefix}/client_temp";
    proxy_temp_path "{prefix}/proxy_temp";
    log_format project '$time_iso8601 $request $status upstream=$upstream_addr';
    access_log "{DATA}/logs/nginx-access.log" project;
    client_max_body_size {LIMITS['file_bytes'] + 65536};
    upstream computes {{
        server 127.0.0.1:{PORTS['compute_1']};
        server 127.0.0.1:{PORTS['compute_2']};
    }}
    server {{
        listen 127.0.0.1:{PORTS['entry']};
        location /api/ {{
            proxy_pass http://127.0.0.1:{PORTS['api']};
            proxy_read_timeout 30s;
            proxy_request_buffering off;
        }}
        location = /health {{ proxy_pass http://127.0.0.1:{PORTS['api']}; }}
        location = /openapi.json {{ proxy_pass http://127.0.0.1:{PORTS['api']}; }}
        location = /docs {{ proxy_pass http://127.0.0.1:{PORTS['api']}; }}
        location / {{
            root "{DATA}/ui";
            try_files $uri $uri/ /index.html;
            add_header Cache-Control "no-cache";
        }}
    }}
    server {{
        listen 127.0.0.1:{PORTS['balancer']};
        location / {{
            proxy_pass http://computes;
            proxy_read_timeout {int(LIMITS['execution_seconds']) + 30}s;
            proxy_next_upstream off;
        }}
    }}
}}
'''
    (prefix / "nginx.conf").write_text(config, encoding="utf-8")
    command(["nginx", "-t", "-p", str(prefix) + "/", "-c", "nginx.conf"], stdout=subprocess.DEVNULL)
    master = owned("nginx")
    if master:
        os.kill(master["pid"], signal.SIGHUP)


def launch(name):
    if owned(name):
        return
    if name == "nginx" and (DATA / "nginx/nginx.pid").exists():
        # Recover a master started just before an interrupted controller wrote its PID record.
        raw_pid = (DATA / "nginx/nginx.pid").read_text().strip()
        # nginx -t can leave an empty private pidfile when no master is running.
        item = identity(int(raw_pid)) if raw_pid else None
        marker = str(DATA / "nginx")
        if item:
            if "nginx" not in item["argv"] or marker not in item["argv"]:
                raise RuntimeError("Private Nginx pidfile identifies a different process")
            atomic_json(DATA / "pids/nginx.json", item | {"marker": marker})
            return
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO / "backend")
    env["MTW_CONFIG"] = str(CONFIG_PATH)
    env["PYTHONUNBUFFERED"] = "1"
    if name == "nginx":
        prefix = DATA / "nginx"
        argv = ["nginx", "-p", str(prefix) + "/", "-c", "nginx.conf", "-g", "daemon off;"]
        marker = str(prefix)
    elif name == "dispatcher":
        argv = [sys.executable, "-m", "mtw.dispatcher"]
        marker = "mtw.dispatcher"
    else:
        argv = [sys.executable, "-m", "mtw.service", name]
        marker = f"mtw.service {name}"
    with (DATA / "logs" / f"{name}.log").open("ab") as log:
        process = subprocess.Popen(argv, cwd=REPO, env=env, start_new_session=True,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 2
    item = identity(process.pid)
    while item is None and process.poll() is None and time.monotonic() < deadline:
        time.sleep(.01)
        item = identity(process.pid)
    if item is None:
        raise RuntimeError(f"{name} exited during launch; inspect {DATA}/logs/{name}.log")
    atomic_json(DATA / "pids" / f"{name}.json", item | {"marker": marker})


def wait_health(port):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1, trust_env=False)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"Service at port {port} did not become ready; inspect {DATA}/logs")


def start(names):
    pg_start()
    write_nginx()
    for name in names:
        launch(name)
        if name in ("api", "compute-1", "compute-2"):
            wait_health(PORTS[name.replace("-", "_")])
    status()


def stop_one(name):
    item = owned(name)
    if not item:
        return
    os.kill(item["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 8
    while owned(name) and time.monotonic() < deadline:
        time.sleep(0.1)
    if owned(name):
        raise RuntimeError(f"{name} did not stop; inspect it before force-stopping")


def stop(names):
    if "dispatcher" in names:
        stop_one("dispatcher")
    # Request cancellation before stopping computes; their normal manager reaps the group.
    if any(name.startswith("compute-") for name in names):
        from .db import Job, Session
        from sqlalchemy import select
        with Session.begin() as session:
            jobs = list(session.scalars(select(Job).where(Job.status == "running", Job.server_id.in_(names))))
            for job in jobs:
                job.cancel_requested = True
        deadline = time.monotonic() + 8
        while jobs and time.monotonic() < deadline:
            with Session() as session:
                jobs = list(session.scalars(select(Job).where(Job.status == "running", Job.server_id.in_(names))))
            time.sleep(0.1)
        if jobs:
            raise RuntimeError("Compute did not acknowledge cancellation; leave services running for inspection")
    for name in reversed(names):
        if name != "dispatcher":
            stop_one(name)
    print(json.dumps({"stopped": list(names), "database_preserved": True}))


def status():
    result = {"postgres": {"running": pg_running(), "port": PORTS["postgres"]}, "services": {}}
    for name in NAMES:
        item = owned(name)
        result["services"][name] = {"running": item is not None, "pid": item["pid"] if item else None}
    for name in ("api", "compute-1", "compute-2"):
        try:
            result["services"][name]["health"] = httpx.get(
                f"http://127.0.0.1:{PORTS[name.replace('-', '_')]}/health", timeout=1, trust_env=False).json()
        except httpx.HTTPError:
            pass
    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["setup", "start", "status", "stop"])
    parser.add_argument("services", nargs="*", choices=list(NAMES) + ["postgres"])
    args = parser.parse_args()
    if args.action == "status":
        return status()
    with control_lock():
        if args.action == "setup":
            return setup()
        names = args.services or list(NAMES)
        postgres = "postgres" in names
        names = [name for name in NAMES if name in names]
        if args.action == "start":
            return start(names)
        stop(names)
        if postgres:
            if any(owned(name) for name in NAMES):
                raise RuntimeError("Stop all project services before stopping the project database")
            if pg_running():
                command([PG_BIN / "pg_ctl", "-D", PGDATA, "-m", "fast", "-w", "stop"], stdout=subprocess.DEVNULL)
                print("Project PostgreSQL stopped; persisted data retained")


if __name__ == "__main__":
    main()
