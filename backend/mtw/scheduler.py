"""Atomically assign FIFO work to the least occupied fresh PostgreSQL slot."""
from datetime import datetime
import json
from uuid import uuid4
from sqlalchemy import select, text
from .db import ComputeServer, Job, Session, now
from .servers import SERVERS, slot_snapshot

ACTIVE = ("dispatching", "running")
MAX_ACTIVE = 3


def reserve_next(session_factory=Session):
    reservation = decision = None
    with session_factory.begin() as session:
        # Shared by all dispatchers; no network inside the reservation transaction.
        session.execute(text("SELECT pg_advisory_xact_lock(7132103)"))
        job = session.scalar(select(Job).where(Job.status == "queued", Job.cancel_requested.is_(False))
                             .order_by(Job.created_at, Job.id).with_for_update().limit(1))
        if job is None:
            return None
        at = now()
        candidates, unresolved = slot_snapshot(session, at, lock=True)
        active = sum(c['running']+c['reserved'] for c in candidates)+unresolved
        eligible = [c for c in candidates if c['eligible']]
        available = [c for c in eligible if c['load'] < 1]
        reason = ('unresolved_legacy_reservation' if unresolved else 'global_slot_limit' if active >= MAX_ACTIVE
                  else 'no_fresh_ready_servers' if not eligible else 'all_eligible_slots_occupied' if not available
                  else 'least_slot_load_then_oldest_assignment')
        selected = None
        if not unresolved and active < MAX_ACTIVE and available:
            def rank(candidate):
                previous = candidate['last_assigned_at']
                return (candidate['load'], datetime.fromisoformat(previous).timestamp() if previous else float('-inf'), candidate['server_id'])
            selected = min(available, key=rank)['server_id']
            job.status = job.stage = 'dispatching'
            job.server_id, job.dispatch_token, job.updated_at = selected, uuid4(), at
            session.get(ComputeServer, selected).last_assigned_at = at
            session.flush()  # Existing unique index also covers dispatching.
            reservation = {'job_id': str(job.id), 'server_id': selected, 'dispatch_token': str(job.dispatch_token)}
        decision = {'event': 'scheduler_decision', 'at': at.isoformat(), 'job_id': str(job.id),
            'server_id': selected, 'reason': reason, 'candidates': candidates,
            'unresolved_reservations': unresolved, 'maximum_active': MAX_ACTIVE}
    # Only committed decisions; no tokens or credentials are logged.
    print(json.dumps(decision, separators=(',', ':')), flush=True)
    return reservation


def outstanding(session_factory=Session):
    with session_factory() as session:
        return [{"job_id": str(j.id), 'server_id': j.server_id, "dispatch_token": str(j.dispatch_token)}
                for j in session.scalars(select(Job).where(Job.status == "dispatching", Job.server_id.in_(SERVERS))
                    .order_by(Job.created_at, Job.id)) if j.dispatch_token is not None]


def still_reserved(reservation, session_factory=Session):
    with session_factory() as session:
        job = session.get(Job, reservation["job_id"])
        return (job is not None and job.status == "dispatching" and job.server_id == reservation.get('server_id')
                and job.server_id in SERVERS
                and str(job.dispatch_token) == reservation["dispatch_token"])
