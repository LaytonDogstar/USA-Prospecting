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
