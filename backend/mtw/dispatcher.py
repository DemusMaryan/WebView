"""Concurrent, restart-safe handoff of PostgreSQL reservations via Nginx."""
import asyncio
import signal
import httpx
from .config import PORTS
from .scheduler import outstanding, reserve_next, still_reserved
from .service_auth import service_headers


async def deliver(client, reservation, stop):
    # Retry the SAME token after an ambiguous HTTP reply. Compute accepts once.
    while not stop.is_set() and still_reserved(reservation):
        try:
            response = await client.post(f"http://127.0.0.1:{PORTS['balancer']}/execute",
                json=reservation, headers=service_headers() | {"X-MTW-Job": reservation["job_id"]})
            print(f"handoff job_id={reservation['job_id']} via=nginx:{PORTS['balancer']} "
                  f"http={response.status_code} body={response.text[:300]}", flush=True)
            if response.status_code not in (200, 202, 409, 502, 503, 504):
                print("Unexpected compute response; reservation retained", flush=True)
        except httpx.HTTPError as error:
            print(f"handoff job_id={reservation['job_id']} retry={type(error).__name__}", flush=True)
        if still_reserved(reservation):
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.25)
            except TimeoutError:
                pass


async def main():
    stop = asyncio.Event()
    for event in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(event, stop.set)
    deliveries = {}
    async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
        try:
            while not stop.is_set():
                for job_id, task in list(deliveries.items()):
                    if task.done():
                        task.result()
                        del deliveries[job_id]
                while reserve_next() is not None:
                    pass
                # Recover committed reservations interrupted before/during POST.
                # Running jobs are never replayed on dispatcher restart.
                for reservation in outstanding():
                    job_id = reservation["job_id"]
                    if job_id not in deliveries:
                        deliveries[job_id] = asyncio.create_task(deliver(client, reservation, stop))
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.2)
                except TimeoutError:
                    pass
        finally:
            # These tasks own only HTTP handoff, never a worker process group.
            for task in deliveries.values():
                task.cancel()
            await asyncio.gather(*deliveries.values(), return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
