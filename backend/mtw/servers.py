"""PostgreSQL readiness/launch reservations. No process creation in transactions."""
from datetime import timedelta
from uuid import uuid4
from sqlalchemy import func, select, text
from .config import CONFIG
from .db import ComputeServer, Job, Session, now

SETTINGS = CONFIG["autoscale"]
BASE = ("compute-1","compute-2")
SERVERS = (*BASE,"compute-3")
SLOT_CAPACITY = 1
if SETTINGS["base_servers"] != 2 or SETTINGS["max_servers"] != 3:
    raise ValueError("This deployment requires exactly 2 baseline and at most 3 compute servers")
if any(SETTINGS[k] <= 0 for k in ("startup_seconds","retry_seconds","max_start_attempts","health_interval_seconds","heartbeat_seconds")):
    raise ValueError("Autoscale limits must be positive")


def ready_servers(session, at=None):
    at = at or now()
    return tuple(session.scalars(select(ComputeServer.server_id).where(ComputeServer.state=="ready",
        ComputeServer.heartbeat_at >= at-timedelta(seconds=SETTINGS["heartbeat_seconds"]))
        .order_by(ComputeServer.server_id)))


def seed_test_baseline(session):
    """Explicit fixture helper; production rows are seeded only by migration."""
    for name in BASE:
        session.add(ComputeServer(server_id=name,kind="native",state="ready",heartbeat_at=now()))


def compute_route(server_id, action='execute'):
    """Trusted allowlist, never a client-controlled upstream URL."""
    if server_id not in SERVERS or action not in ('execute', 'health'):
        raise ValueError('Unknown internal compute route')
    return f'/internal/compute/{server_id}/{action}'


def slot_snapshot(session, at=None, lock=False):
    """Common PostgreSQL accounting: running plus assigned deliveries."""
    at = at or now()
    query = select(ComputeServer).order_by(ComputeServer.server_id)
    if lock:
        query = query.with_for_update()
    rows = list(session.scalars(query))
    counts = list(session.execute(select(Job.server_id, Job.status, func.count())
        .where(Job.status.in_(('dispatching', 'running'))).group_by(Job.server_id, Job.status)))
    registered = {row.server_id for row in rows}
    unresolved = sum(count for name, _, count in counts if name not in SERVERS or name not in registered)
    result = []
    for row in rows:
        fresh = row.heartbeat_at is not None and row.heartbeat_at >= at-timedelta(seconds=SETTINGS['heartbeat_seconds'])
        running = sum(count for name, state, count in counts if name == row.server_id and state == 'running')
        reserved = sum(count for name, state, count in counts if name == row.server_id and state == 'dispatching')
        eligible = row.server_id in SERVERS and row.state == 'ready' and fresh
        reason = 'not_ready' if row.state != 'ready' else 'stale_heartbeat' if not fresh else 'unknown_server' if row.server_id not in SERVERS else 'full' if running+reserved >= SLOT_CAPACITY else 'available'
        result.append({'server_id': row.server_id, 'state': row.state, 'heartbeat_fresh': fresh,
            'capacity': SLOT_CAPACITY, 'running': running, 'reserved': reserved,
            'load': (running+reserved)/SLOT_CAPACITY, 'eligible': eligible, 'reason': reason,
            'last_assigned_at': row.last_assigned_at.isoformat() if row.last_assigned_at else None})
    return result, unresolved


def reserve_launch(session_factory=Session):
    with session_factory.begin() as s:
        s.execute(text("SELECT pg_advisory_xact_lock(7132104)"))
        extra = s.get(ComputeServer,"compute-3",with_for_update=True)
        if not SETTINGS["enabled"] or extra is None or extra.state in {"starting","ready","stopping"}:
            return None
        if extra.attempts >= SETTINGS["max_start_attempts"] or (extra.retry_after and extra.retry_after > now()):
            return None
        if not all(k in ready_servers(s) for k in BASE): return None
        busy = set(s.scalars(select(Job.server_id).where(Job.status.in_(('running','dispatching')),Job.server_id.in_(BASE))))
        queued = s.scalar(select(Job.id).where(Job.status=="queued",Job.cancel_requested.is_(False)).limit(1))
        if busy != set(BASE) or queued is None: return None
        extra.state,extra.launch_token,extra.started_at = "starting",uuid4(),now()
        extra.attempts += 1
        extra.error,extra.process = None,None
        return str(extra.launch_token)


def launch_failed(token, message, session_factory=Session):
    with session_factory.begin() as s:
        row=s.get(ComputeServer,"compute-3",with_for_update=True)
        if str(row.launch_token) != token: return
        row.state,row.error = "failed",message[:1000]
        row.heartbeat_at = None
        row.retry_after = now()+timedelta(seconds=SETTINGS["retry_seconds"])
