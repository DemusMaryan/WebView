"""One persistent queue consumer, with a database-wide single-dispatcher lock."""
import asyncio
import signal
from uuid import uuid4
import httpx
from sqlalchemy import select, text
from .config import PORTS, TERMINAL
from .db import Job, Session, engine, now


async def main():
    stop = asyncio.Event()
    for event in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(event, stop.set)
    # A session advisory lock survives transaction commits; a second dispatcher exits.
    with engine.connect() as lease:
        if not lease.scalar(text("SELECT pg_try_advisory_lock(7132102)")):
            raise RuntimeError("Another dispatcher owns the single execution slot")
        lease.commit()
        with Session.begin() as session:
            abandoned = list(session.scalars(select(Job).where(Job.status == "dispatching").with_for_update()))
            for job in abandoned:
                job.status = job.stage = "failed"
                job.error = "Dispatcher restarted before compute accepted reservation; not replayed"
                job.finished_at = job.updated_at = now()
        async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
            while not stop.is_set():
                # A lost DB connection also loses the advisory lock: exit on that failure.
                lease.execute(text("SELECT 1"))
                lease.commit()
                with Session.begin() as session:
                    running = session.scalar(select(Job).where(Job.status.in_(["running", "dispatching"])).order_by(Job.created_at).limit(1))
                    if running:
                        # Never replay an ambiguous in-flight request after dispatcher restart.
                        job_id = None
                    else:
                        job = session.scalar(select(Job).where(Job.status == "queued", Job.cancel_requested.is_(False))
                                             .order_by(Job.created_at).with_for_update(skip_locked=True).limit(1))
                        job_id = job.id if job else None
                        if job:
                            job.status = job.stage = "dispatching"
                            job.dispatch_token = uuid4()
                            job.updated_at = now()
                            token = job.dispatch_token
                if job_id:
                    print(f"dispatch job_id={job_id} via=nginx:{PORTS['balancer']}", flush=True)
                    try:
                        delivery = asyncio.create_task(client.post(f"http://127.0.0.1:{PORTS['balancer']}/execute",
                            json={"job_id": str(job_id), "dispatch_token": str(token)}))
                        stopping = asyncio.create_task(stop.wait())
                        await asyncio.wait([delivery, stopping], return_when=asyncio.FIRST_COMPLETED)
                        if stopping.done():
                            delivery.cancel()
                            await asyncio.gather(delivery, return_exceptions=True)
                            break
                        stopping.cancel()
                        await asyncio.gather(stopping, return_exceptions=True)
                        response = delivery.result()
                        response.raise_for_status()
                        print(response.text, flush=True)
                    except Exception as error:
                        with Session.begin() as session:
                            job = session.scalar(select(Job).where(Job.id == job_id).with_for_update())
                            if job.status == "dispatching":
                                job.status = job.stage = "failed"
                                job.error = f"Dispatch failed before compute claimed job: {type(error).__name__}: {error}"
                                job.finished_at = job.updated_at = now()
                            # If compute accepted it, its persisted status is authoritative.
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.2)
                except TimeoutError:
                    pass


if __name__ == "__main__":
    asyncio.run(main())
