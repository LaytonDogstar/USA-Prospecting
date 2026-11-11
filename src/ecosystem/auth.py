"""Username/password accounts (argon2 hashes). The login pages arrive with the web app in Phase 2."""
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
from sqlalchemy import select

from . import models as m

_ph = PasswordHasher()
MIN_PASSWORD_LENGTH = 12
MAX_FAILED = 5
LOCK_MINUTES = 15


def create_user(s, username: str, password: str, role: str = "viewer") -> m.AppUser:
    if role not in m.USER_ROLES:
        raise ValueError(f"role must be one of {m.USER_ROLES}")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    username = username.strip().lower()
    if s.scalar(select(m.AppUser.id).where(m.AppUser.username == username)):
        raise ValueError("username already exists")
    u = m.AppUser(username=username, password_hash=_ph.hash(password), role=role)
    s.add(u)
    s.add(m.AuditLog(actor="cli", action="user.create", table_name="app_user", after={"username": username, "role": role}))
    s.flush()
    return u


def authenticate(s, username: str, password: str) -> m.AppUser | None:
    """Returns the user on success. Locks the account for 15 minutes after 5 failures in a row."""
    u = s.scalar(select(m.AppUser).where(m.AppUser.username == username.strip().lower()))
    now = datetime.now(timezone.utc)
    if u is None or not u.is_active:
        _ph.hash(password)  # similar timing whether or not the user exists
        return None
    if u.locked_until and u.locked_until > now:
        return None
    try:
        _ph.verify(u.password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        u.failed_logins += 1
        if u.failed_logins >= MAX_FAILED:
            u.locked_until, u.failed_logins = now + timedelta(minutes=LOCK_MINUTES), 0
        return None
    if _ph.check_needs_rehash(u.password_hash):
        u.password_hash = _ph.hash(password)
    u.failed_logins, u.locked_until, u.last_login_at = 0, None, now
    return u
