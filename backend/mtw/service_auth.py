"""Private service credential; no browser session can substitute for it."""
from functools import lru_cache
import os
import re
import secrets
from fastapi import HTTPException, Request
from .config import DATA

KEY_FILE = DATA / "service-key"


def ensure_service_key():
    try:
        fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as target:
            target.write(secrets.token_hex(32) + "\n")
    service_key()


@lru_cache(maxsize=1)
def service_key():
    if KEY_FILE.is_symlink() or KEY_FILE.stat().st_mode & 0o077:
        raise RuntimeError("Service credential must be a private native file (0600)")
    value = KEY_FILE.read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise RuntimeError("Invalid service credential file")
    return value


def service_headers():
    return {"X-MTW-Service-Key": service_key()}


def require_service(request: Request):
    if not secrets.compare_digest(request.headers.get("x-mtw-service-key", "").encode(), service_key().encode()):
        raise HTTPException(401, "Service authentication required")
