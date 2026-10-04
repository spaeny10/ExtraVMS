"""setpassword replaces the hash, enforces the minimum length and ends the user's sessions."""
import pytest

from hub import auth, db


def test_set_password():
    db.engine()
    u = auth.create_user("who@example.com", "first-password-1")
    with db.engine().begin() as c:
        c.execute(db.sessions.insert().values(id="s-setpw", user_id=u["id"], created_at=0, expires_at=10 ** 12, ip="", ua=""))
    with pytest.raises(ValueError):
        auth.set_password("who@example.com", "short")
    with pytest.raises(ValueError):
        auth.set_password("nobody@example.com", "long-enough-password")
    auth.set_password("who@example.com", "second-password-2")
    fresh = auth.user_by_email("who@example.com")
    assert auth.verify_password("second-password-2", fresh["password_hash"])
    assert not auth.verify_password("first-password-1", fresh["password_hash"])
    with db.engine().connect() as c:
        assert c.execute(db.sessions.select().where(db.sessions.c.user_id == u["id"])).fetchall() == []
