"""Accept TLS forwarding only from the configured loopback proxy with its key."""
from functools import lru_cache
import os
import re
import secrets
from urllib.parse import urlsplit
from starlette.requests import Request
from .config import CONFIG, DATA

KEY_FILE = DATA / "proxy-key"


def ensure_proxy_key():
    try:
        fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as target:
            target.write(secrets.token_hex(32) + "\n")
    proxy_key()


@lru_cache(maxsize=1)
def proxy_key():
    if KEY_FILE.is_symlink() or KEY_FILE.stat().st_mode & 0o077 or KEY_FILE.stat().st_uid != os.getuid():
        raise RuntimeError("Proxy credential must be a private native file (0600)")
    value = KEY_FILE.read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise RuntimeError("Invalid private proxy credential")
    return value


class TrustedTLSProxy:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        settings = CONFIG["tls"]
        forwarded = request.headers.get("x-mtw-proxy-key", "")
        authority = urlsplit(settings["canonical_origin"]).netloc
        if ((scope.get("client") or (None,))[0] in settings["trusted_proxy_addresses"] and forwarded
                and request.headers.get("x-forwarded-proto") == "https"
                and request.headers.get("x-forwarded-host") == authority
                and request.headers.get("host") == authority):
            try:
                trusted = secrets.compare_digest(forwarded.encode(), proxy_key().encode())
            except (OSError, RuntimeError):
                trusted = False  # Fail closed if the local credential cannot be read.
            if trusted:
                scope = scope | {"scheme": "https"}
        await self.app(scope, receive, send)
