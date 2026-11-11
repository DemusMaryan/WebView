"""FIFO queue estimates from bounded PostgreSQL metadata, never JSONL or geometry."""
import heapq
import json
import math
import statistics
import subprocess
from sqlalchemy import select
from .config import CONFIG
from .db import Job, now
from .scheduler import ACTIVE
from .servers import SERVERS, slot_snapshot

SETTINGS = CONFIG["queue"]


def positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


if (not all(positive(SETTINGS[k]) for k in ("reference_frames", "reference_width", "reference_height"))
        or type(SETTINGS["history_samples"]) is not int or not 1 <= SETTINGS["history_samples"] <= 100
        or not isinstance(SETTINGS.get("initial_seconds"), (int, float))
        or not math.isfinite(SETTINGS["initial_seconds"]) or SETTINGS["initial_seconds"] < 0):
    raise ValueError("Invalid queue estimation settings; initial_seconds=0 disables the cold-start fallback")


def probe_video(path):
    """Bounded header hint only. Decoder still enforces all actual limits/PTS."""
    try:
        result = subprocess.run(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
            "-select_streams", "v", "-show_entries", "stream=width,height,nb_frames,duration,avg_frame_rate",
            "-of", "json", str(path)], capture_output=True, text=True, timeout=2)
        streams = json.loads(result.stdout).get("streams", []) if result.returncode == 0 else []
        if len(streams) != 1:
            return None
        stream = streams[0]
        width, height = int(stream["width"]), int(stream["height"])
        frames = int(stream.get("nb_frames", 0)) if str(stream.get("nb_frames", "0")).isdigit() else 0
        if not frames:
            numerator, denominator = map(float, stream["avg_frame_rate"].split("/"))
            frames = round(float(stream["duration"]) * numerator / denominator)
        if not (0 < frames <= 10_000_000 and 0 < width <= 16384 and 0 < height <= 16384):
            return None
        return {"frames": frames, "width": width, "height": height, "source": "container_header_hint"}
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, ZeroDivisionError, OverflowError):
        return None


def work(job):
    info = job.video_info or {}
    calibration = job.parameters.get("calibration", {})
    frames = job.total_frames or info.get("frames")
    width, height = info.get("width", calibration.get("width")), info.get("height", calibration.get("height"))
    if not all(positive(v) for v in (frames, width, height)):
        return None
    return frames * width * height / (SETTINGS["reference_frames"] * SETTINGS["reference_width"] * SETTINGS["reference_height"])


def estimate_queue(session, target, at=None):
    empty = {"queue_position": None, "estimated_wait_seconds": None}
    if target.status != "queued" or target.cancel_requested:
        return empty
    at = at or now()
    queued = list(session.scalars(select(Job).where(Job.status == "queued", Job.cancel_requested.is_(False))
        .order_by(Job.created_at, Job.id)))
    position = next((i + 1 for i, job in enumerate(queued) if job.id == target.id), None)
    if position is None:
        return empty
    history = list(session.scalars(select(Job).where(Job.status == "succeeded", Job.execution_seconds > 0)
        .order_by(Job.finished_at.desc().nulls_last(), Job.id.desc()).limit(SETTINGS["history_samples"])))
    rates = [job.execution_seconds / units for job in history
             if positive(job.execution_seconds) and (units := work(job)) is not None]
    rate = statistics.median(rates) if rates else None
    initial = SETTINGS.get("initial_seconds")

    def duration(job):
        units = work(job)
        value = rate * units if rate is not None and units is not None else initial * (units or 1) if positive(initial) else None
        return value if positive(value) else None

    active = list(session.scalars(select(Job).where(Job.status.in_(ACTIVE))))
    candidates, unresolved = slot_snapshot(session, at)
    ready = [c for c in candidates if c['eligible']]
    if not ready or unresolved:
        return {"queue_position": position, "estimated_wait_seconds": None}
    # Reservations occupy their assigned lane, even before a worker starts.
    # An unavailable server's work must not consume a different ready lane.
    by_server = {j.server_id: j for j in active if j.server_id in SERVERS}
    slots = []
    for candidate in ready:
        job = by_server.get(candidate['server_id'])
        if job is None:
            slots.append(0.0)
            continue
        predicted = duration(job)
        elapsed = max(0, (at - job.started_at).total_seconds()) if job.status == 'running' and job.started_at else 0
        remaining = predicted - elapsed if predicted is not None else None
        if predicted is not None and job.total_frames and job.stage == "processing":
            fraction = max(0, min(1, job.processed_frames / job.total_frames))
            remaining = max(remaining, predicted * (1 - fraction))
        # Overdue jobs without usable progress (or failed termination) have no
        # defensible completion estimate. Another known slot can still serve us.
        if remaining is None or remaining <= 0 or job.stage == "termination_failed":
            remaining = math.inf
        slots.append(remaining)
    heapq.heapify(slots)
    for job in queued[:position]:
        available = heapq.heappop(slots)
        if job.id == target.id:
            return {"queue_position": position,
                    "estimated_wait_seconds": round(max(0, available), 1) if math.isfinite(available) else None}
        predicted = duration(job)
        heapq.heappush(slots, available + predicted if predicted is not None else math.inf)
