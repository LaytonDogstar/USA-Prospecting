import pytest

from ecosystem.auth import MAX_FAILED, authenticate, create_user


def test_password_login_and_lockout(session):
    create_user(session, "Alice", "correct horse battery", "admin")
    with pytest.raises(ValueError):
        create_user(session, "alice", "another long password")
    with pytest.raises(ValueError):
        create_user(session, "bob", "short")
    assert authenticate(session, "ALICE", "correct horse battery").role == "admin"
    assert authenticate(session, "alice", "wrong") is None
    assert authenticate(session, "nobody", "x") is None
    for _ in range(MAX_FAILED):
        authenticate(session, "alice", "wrong")
    assert authenticate(session, "alice", "correct horse battery") is None  # locked


def test_bootstrap_admin_never_crashes_on_a_short_password(migrated, monkeypatch):
    from sqlalchemy import text

    from ecosystem import cli
    from ecosystem.config import database_url

    with migrated.begin() as c:
        c.execute(text("TRUNCATE app_user RESTART IDENTITY CASCADE"))
    monkeypatch.setenv("DATABASE_URL", database_url(test=True).replace("postgresql+psycopg://", "postgresql://"))
    monkeypatch.setenv("ADMIN_USERNAME", "first")
    monkeypatch.setenv("ADMIN_PASSWORD", "short")
    assert "admin NOT created" in cli.bootstrap_admin()
    monkeypatch.setenv("ADMIN_PASSWORD", "long-enough-password")
    assert cli.bootstrap_admin() == "created admin first"
    assert cli.bootstrap_admin() == "users already exist; nothing to do"
