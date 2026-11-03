"""PostgreSQL sessions and session-bound CSRF, without logging credentials."""
from datetime import timedelta
import hashlib
import re
import secrets
from threading import BoundedSemaphore
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from .config import CONFIG
from .db import AuthSession, Session, User, now

SETTINGS = CONFIG["auth"]
COOKIE = "mtw_session"


def validate_settings(settings):
    mode = settings["cookie_mode"]
    if mode not in {"localhost-http", "https"}:
        raise ValueError("auth.cookie_mode must explicitly be localhost-http or https")
    if not settings["allowed_origins"]:
        raise ValueError("Explicit allowed origins are required")
    for value in settings["allowed_origins"]:
        origin = urlsplit(value)
        if (origin.path or origin.query or origin.fragment or origin.username or origin.password
                or not origin.hostname or origin.scheme not in {"http", "https"}):
            raise ValueError("Invalid auth origin")
        if mode == "localhost-http" and (origin.scheme != "http" or origin.hostname not in {"127.0.0.1", "localhost"}):
            raise ValueError("HTTP dev cookies are allowed only for explicit loopback origins")
        if mode == "https" and origin.scheme != "https":
            raise ValueError("HTTPS cookie mode requires HTTPS origins and Secure cookies")
    if not (60 <= settings["session_seconds"] <= 604800 and 60 <= settings["preauth_seconds"] <= 3600):
        raise ValueError("Invalid server session lifetime")


validate_settings(SETTINGS)
hasher = PasswordHasher()  # argon2-cffi's Argon2id / RFC 9106 low-memory defaults.
hash_slots = BoundedSemaphore(2)
dummy_hash = hasher.hash(secrets.token_urlsafe(32))
router = APIRouter(prefix="/api/auth")


def token_hash(token):
    return hashlib.sha256(b"session\0" + token.encode("ascii")).hexdigest()


def csrf_token(token):
    # Domain-separated, one-way derivation: knowing CSRF cannot recover the cookie.
    return hashlib.sha256(b"csrf\0" + token.encode("ascii")).hexdigest()


def user_value(user):
    return {"id": str(user.id), "login": user.login}


def identity(token):
    if not token or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        return None
    digest = token_hash(token)
    with Session() as session:
        record = session.get(AuthSession, digest)
        if record is None or record.revoked_at is not None or record.expires_at <= now():
            return None
        user = session.get(User, record.user_id) if record.user_id else None
        return {"token_hash": digest, "user": user_value(user) if user else None,
                "csrf_token": csrf_token(token), "remaining_seconds": max(1, int((record.expires_at - now()).total_seconds()))}


def error(status, code, message):
    return HTTPException(status, {"code": code, "message": message})


class BrowserSecurity:
    """Check before multipart parsing; every /api/jobs subpath is protected."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        request = Request(scope)
        try:
            if SETTINGS["cookie_mode"] == "https" and request.url.scheme != "https":
                raise error(400, "https_required", "Use the canonical HTTPS application URL")
            origin = request.headers.get("origin")
            if origin is not None and origin not in SETTINGS["allowed_origins"]:
                raise error(403, "csrf_rejected", "Origin is not allowed")
            if request.headers.get("sec-fetch-site") == "cross-site":
                raise error(403, "csrf_rejected", "Cross-site requests are not allowed")
            current = await run_in_threadpool(identity, request.cookies.get(COOKIE))
            scope.setdefault("state", {})["auth"] = current
            path = scope["path"]
            bootstrap = path == "/api/auth/session" and request.method == "GET"
            login = path in {"/api/auth/login", "/api/auth/register"} and request.method == "POST"
            if not bootstrap and not login and (not current or current["user"] is None):
                raise error(401, "authentication_required", "Sign in to access this resource")
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                supplied = request.headers.get("x-csrf-token", "")
                if (origin not in SETTINGS["allowed_origins"] or not current or
                        not secrets.compare_digest(supplied.encode(), current["csrf_token"].encode())):
                    raise error(403, "csrf_rejected", "A valid session CSRF header and Origin are required")
        except HTTPException as exception:
            return await JSONResponse({"detail": exception.detail}, status_code=exception.status_code)(scope, receive, send)
        async def private_send(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                message = message | {"headers": headers + [(b"cache-control", b"private, no-store"),
                                                           (b"x-content-type-options", b"nosniff")]}
            await send(message)
        await self.app(scope, receive, private_send)


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login: str = Field(min_length=3, max_length=32, pattern=r"^[a-z0-9_][a-z0-9_.-]*$")
    password: str = Field(min_length=12, max_length=128)

    @field_validator("login", mode="before")
    @classmethod
    def normalize_login(cls, value):
        return value.strip().lower() if isinstance(value, str) else value


def set_cookie(response, token, seconds):
    response.set_cookie(COOKIE, token, max_age=seconds, httponly=True,
                        secure=SETTINGS["cookie_mode"] == "https", samesite="strict", path="/")
    response.headers["Cache-Control"] = "no-store"


def issue(session, request, response, user=None):
    if SETTINGS["cookie_mode"] == "https" and request.url.scheme != "https":
        raise error(400, "https_required", "HTTPS is required for Secure sessions")
    previous = request.state.auth
    if previous:
        record = session.get(AuthSession, previous["token_hash"])
        if record:
            record.revoked_at = now()
    token = secrets.token_urlsafe(32)
    seconds = SETTINGS["session_seconds"] if user else SETTINGS["preauth_seconds"]
    session.add(AuthSession(token_hash=token_hash(token), user_id=user.id if user else None,
                            expires_at=now() + timedelta(seconds=seconds)))
    set_cookie(response, token, seconds)
    return {"user": user_value(user) if user else None, "csrf_token": csrf_token(token)}


@router.get("/session")
def bootstrap(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    current = request.state.auth
    if current:
        # Upgrade an existing dev cookie to Secure without extending DB expiry.
        set_cookie(response, request.cookies[COOKIE], current["remaining_seconds"])
        return {"user": current["user"], "csrf_token": current["csrf_token"]}
    with Session.begin() as session:
        return issue(session, request, response)


@router.post("/register", status_code=201)
def register(credentials: Credentials, request: Request, response: Response):
    with hash_slots:
        encoded = hasher.hash(credentials.password)
    try:
        with Session.begin() as session:
            user = User(login=credentials.login, password_hash=encoded)
            session.add(user)
            session.flush()
            return issue(session, request, response, user)
    except IntegrityError:
        raise error(409, "login_unavailable", "This login is already registered") from None


@router.post("/login")
def login(credentials: Credentials, request: Request, response: Response):
    with Session() as session:
        user = session.scalar(select(User).where(User.login == credentials.login))
    try:
        with hash_slots:
            hasher.verify(user.password_hash if user else dummy_hash, credentials.password)
    except (VerificationError, InvalidHashError):
        raise error(401, "invalid_credentials", "Incorrect login or password") from None
    if user is None:
        raise error(401, "invalid_credentials", "Incorrect login or password")
    with Session.begin() as session:
        user = session.get(User, user.id)
        if hasher.check_needs_rehash(user.password_hash):
            with hash_slots:
                user.password_hash = hasher.hash(credentials.password)
        return issue(session, request, response, user)


@router.get("/me")
def me(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return request.state.auth["user"]


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response):
    with Session.begin() as session:
        session.get(AuthSession, request.state.auth["token_hash"]).revoked_at = now()
    response.delete_cookie(COOKIE, httponly=True, secure=SETTINGS["cookie_mode"] == "https", samesite="strict", path="/")
    response.headers["Cache-Control"] = "no-store"
