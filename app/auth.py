from collections.abc import Callable

from fastapi import Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, TimestampSigner
from pwdlib import PasswordHash
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp

from app.config import Settings
from app.db.models import User
from app.db.session import SessionLocal
from app.models import SessionUser, UserRole

COOKIE_NAME = "session"
password_hash = PasswordHash.recommended()


def signer_for(settings: Settings) -> TimestampSigner:
    return TimestampSigner(settings.session_secret)


def hash_password(plain: str) -> str:
    return password_hash.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return password_hash.verify(plain, hashed)


def session_user_from_row(row: User) -> SessionUser:
    return SessionUser(id=row.id, username=row.username, role=row.role)


def bootstrap_user(db: Session, settings: Settings) -> None:
    existing = db.query(User).filter(User.username == settings.dashboard_username).one_or_none()
    if existing is None:
        db.add(
            User(
                username=settings.dashboard_username,
                password_hash=hash_password(settings.dashboard_password),
                role=UserRole.admin.value,
                is_active=True,
            )
        )
        db.commit()
        return
    existing.role = UserRole.admin.value
    existing.is_active = True
    if not verify_password(settings.dashboard_password, existing.password_hash):
        existing.password_hash = hash_password(settings.dashboard_password)
    db.commit()


def create_session_value(settings: Settings, username: str) -> str:
    return signer_for(settings).sign(username.encode("utf-8")).decode("utf-8")


def read_session_value(settings: Settings, value: str) -> str | None:
    try:
        raw = signer_for(settings).unsign(value.encode("utf-8"), max_age=60 * 60 * 24 * 14)
    except BadSignature:
        return None
    return raw.decode("utf-8")


def load_session_user(db: Session, username: str | None) -> SessionUser | None:
    if not username:
        return None
    row = db.query(User).filter(User.username == username).one_or_none()
    if row is None or not row.is_active:
        return None
    return session_user_from_row(row)


def current_user(request: Request) -> SessionUser | None:
    user = getattr(request.state, "session_user", None)
    if not user:
        return None
    return user


class SessionMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        cookie = request.cookies.get(COOKIE_NAME)
        username = read_session_value(self.settings, cookie) if cookie else None
        db = SessionLocal()
        try:
            session_user = load_session_user(db, username)
        finally:
            db.close()
        request.state.session_user = session_user
        request.state.username = session_user["username"] if session_user else None
        return await call_next(request)


def require_login(request: Request) -> SessionUser | RedirectResponse:
    user = current_user(request)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    return user


PUBLIC_PATHS = {"/login", "/health", "/static", "/upwork/callback"}


def is_public_path(path: str) -> bool:
    return (
        path == "/login"
        or path == "/health"
        or path == "/upwork/callback"
        or path.startswith("/static/")
    )
