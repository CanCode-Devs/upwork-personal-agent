from collections.abc import Callable

from fastapi import Request
from sqlalchemy.orm import Session

from app.auth import current_user
from app.config import Settings, get_settings
from app.db.models import User
from app.models import Permission, PermissionFlags, SessionUser, UserRole


class ForbiddenError(Exception):
    pass


class UserMutationError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    UserRole.admin.value: frozenset(Permission),
    UserRole.reviewer.value: frozenset(
        {
            Permission.review,
            Permission.submit,
            Permission.messages,
            Permission.poll,
        }
    ),
}


def has_permission(role: str, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


def flags_for(user: SessionUser | None) -> PermissionFlags:
    role = user["role"] if user else ""
    return PermissionFlags(
        review=has_permission(role, Permission.review),
        submit=has_permission(role, Permission.submit),
        messages=has_permission(role, Permission.messages),
        poll=has_permission(role, Permission.poll),
        settings=has_permission(role, Permission.settings),
        writer=has_permission(role, Permission.writer),
        portfolio=has_permission(role, Permission.portfolio),
        upwork_connect=has_permission(role, Permission.upwork_connect),
        users=has_permission(role, Permission.users),
    )


def require_permission(permission: Permission) -> Callable[[Request], SessionUser]:
    def _check(request: Request) -> SessionUser:
        user = current_user(request)
        if user is None:
            raise RuntimeError("unauthenticated")
        if not has_permission(user["role"], permission):
            raise ForbiddenError()
        return user

    return _check


def is_bootstrap_username(username: str, settings: Settings | None = None) -> bool:
    return username == (settings or get_settings()).dashboard_username


def active_admin_count(db: Session) -> int:
    return int(
        db.query(User)
        .filter(User.role == UserRole.admin.value, User.is_active.is_(True))
        .count()
    )


def _require_user(db: Session, user_id: int) -> User:
    row = db.query(User).filter(User.id == user_id).one_or_none()
    if row is None:
        raise UserMutationError("missing")
    return row


def create_dashboard_user(db: Session, username: str, password_hash: str, role: str) -> User:
    name = username.strip()
    if not name:
        raise UserMutationError("empty")
    if role not in {UserRole.admin.value, UserRole.reviewer.value}:
        raise UserMutationError("invalid_role")
    existing = db.query(User).filter(User.username == name).one_or_none()
    if existing is not None:
        raise UserMutationError("duplicate")
    row = User(username=name, password_hash=password_hash, role=role, is_active=True)
    db.add(row)
    db.flush()
    return row


def set_user_role(db: Session, user_id: int, role: str, settings: Settings | None = None) -> User:
    if role not in {UserRole.admin.value, UserRole.reviewer.value}:
        raise UserMutationError("invalid_role")
    row = _require_user(db, user_id)
    if is_bootstrap_username(row.username, settings):
        raise UserMutationError("owner")
    if (
        row.role == UserRole.admin.value
        and role != UserRole.admin.value
        and row.is_active
        and active_admin_count(db) <= 1
    ):
        raise UserMutationError("last_admin")
    row.role = role
    return row


def set_user_active(db: Session, user_id: int, active: bool, settings: Settings | None = None) -> User:
    row = _require_user(db, user_id)
    if is_bootstrap_username(row.username, settings):
        raise UserMutationError("owner")
    if (
        row.role == UserRole.admin.value
        and row.is_active
        and not active
        and active_admin_count(db) <= 1
    ):
        raise UserMutationError("last_admin")
    row.is_active = active
    return row


def reset_user_password(db: Session, user_id: int, password_hash: str) -> User:
    row = _require_user(db, user_id)
    if not password_hash:
        raise UserMutationError("empty")
    row.password_hash = password_hash
    return row
