import os
from pathlib import Path
import tomllib

REPO = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(os.environ.get("MTW_CONFIG", REPO / "config/local.toml")).resolve()
CONFIG = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
CV_ROOT = Path(CONFIG["paths"]["cv_root"]).expanduser().resolve()
DATA = Path(CONFIG["paths"]["data_root"]).expanduser().resolve()
if any(str(p).startswith("/mnt/") or p == Path("/") for p in (CV_ROOT, DATA)):
    raise RuntimeError("Runtime data must be on the native WSL filesystem")
PORTS = CONFIG["ports"]
LIMITS = CONFIG["limits"]
if type(LIMITS["stored_jobs"]) is not int or LIMITS["stored_jobs"] < 1:
    raise ValueError("limits.stored_jobs must be a positive integer")
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out"}
for directory in (DATA, DATA / "jobs", DATA / "logs", DATA / "pids"):
    directory.mkdir(parents=True, exist_ok=True)


def database_url():
    return (DATA / "database-url").read_text(encoding="utf-8").strip()


def job_dir(job_id):
    from uuid import UUID
    return DATA / "jobs" / str(UUID(str(job_id)))
