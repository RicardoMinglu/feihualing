import concurrent.futures
import hashlib
import sqlite3
import threading
import time

import pytest

from app.accounts import Accounts, AuthError


@pytest.fixture
def accounts(tmp_path):
    return Accounts(tmp_path / "accounts.sqlite3")


def test_bootstrap_reuses_session_and_never_accepts_public_id(accounts):
    guest, token = accounts.bootstrap()
    same, replacement = accounts.bootstrap(token)
    assert same["id"] == guest["id"] and replacement is None
    assert accounts.resolve(token)["id"] == guest["id"]
    assert accounts.resolve(guest["id"]) is None
    other, _ = accounts.bootstrap(guest["id"])
    assert other["id"] != guest["id"]
    with sqlite3.connect(accounts.path) as db:
        stored = db.execute("SELECT token_hash FROM sessions").fetchall()
    assert (token,) not in stored
    assert (hashlib.sha256(token.encode()).hexdigest(),) in stored


def test_unknown_legacy_cookie_never_claims_an_identity(accounts):
    token = "a" * 64
    first, _ = accounts.bootstrap(legacy_token=token)
    second, _ = accounts.bootstrap(legacy_token=token)
    assert first["id"] != second["id"]
    with sqlite3.connect(accounts.path) as db:
        assert db.execute("SELECT count(*) FROM legacy_identities").fetchone()[0] == 0


def legacy_database(path):
    owner = "b" * 64
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE games(id TEXT PRIMARY KEY,owner TEXT,state TEXT)")
        db.execute("INSERT INTO games VALUES('existing-game',?,'{\"untouched\":true}')", (owner,))
        db.execute("CREATE TABLE game_summaries(game_id TEXT PRIMARY KEY,owner TEXT,created_at REAL,ended_at REAL)")
        db.execute("INSERT INTO game_summaries VALUES('existing-game',?,10,20)", (owner,))
        db.execute("CREATE TABLE visitor_profiles(owner TEXT PRIMARY KEY,education_stage TEXT,created_at REAL,updated_at REAL)")
        db.execute("INSERT INTO visitor_profiles VALUES(?,'primary',5,30)", (owner,))
    return owner


def test_legacy_migration_preserves_records_and_revokes_cookie_on_upgrade(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    legacy = legacy_database(path)
    accounts = Accounts(path)
    guest, token = accounts.bootstrap(legacy_token=legacy)
    assert guest["has_records"] is True
    assert guest["created_at"] == 5
    assert guest["last_active_at"] == 30
    again = Accounts(path)
    assert again.bootstrap(legacy_token=legacy)[0]["id"] == guest["id"]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM users").fetchone()[0] == 1
        assert db.execute("SELECT id,owner,state FROM games").fetchone() == ("existing-game", guest["id"], '{"untouched":true}')
        assert db.execute("SELECT owner FROM game_summaries").fetchone()[0] == guest["id"]
        assert db.execute("SELECT owner,education_stage FROM visitor_profiles").fetchone() == (guest["id"], "primary")
    account, upgraded = accounts.upgrade(guest["id"], "诗词用户", "a secure password")
    assert account["id"] == guest["id"] and account["has_records"]
    assert accounts.resolve(token) is None
    assert accounts.resolve(upgraded)["id"] == guest["id"]
    assert accounts.bootstrap(legacy_token=legacy)[0]["id"] != guest["id"]
    assert accounts.resolve(legacy) is None


def test_migration_is_concurrency_safe(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    legacy = legacy_database(path)
    ready = threading.Barrier(4, timeout=5)
    def migrate(_):
        ready.wait()
        return Accounts(path).bootstrap(legacy_token=legacy)[0]["id"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(migrate, range(4)))
    assert len(set(results)) == 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM users").fetchone()[0] == 1


def test_login_cross_device_change_and_logout(accounts):
    guest, old = accounts.bootstrap()
    account, first = accounts.upgrade(guest["id"], "Poet_1", "first-password")
    same, second = accounts.login("poet_1", "first-password")
    assert same["id"] == account["id"]
    assert accounts.resolve(first) is not None and accounts.resolve(second) is not None
    assert accounts.resolve(old) is None
    with pytest.raises(AuthError, match="当前密码错误"):
        accounts.change_credentials(account["id"], "wrong-password", password="second-password")
    renamed, fresh = accounts.change_credentials(account["id"], "first-password", username="李白", password="second-password")
    assert renamed["username"] == "李白"
    assert accounts.resolve(first) is None and accounts.resolve(second) is None
    with pytest.raises(AuthError):
        accounts.login("Poet_1", "first-password")
    assert accounts.login("李白", "second-password")[0]["id"] == account["id"]
    accounts.logout(fresh)
    assert accounts.resolve(fresh) is None
    with sqlite3.connect(accounts.path) as db:
        encoded = db.execute("SELECT password_hash FROM users WHERE id=?", (account["id"],)).fetchone()[0]
    assert encoded.startswith("$argon2id$")
    assert "second-password" not in encoded


def test_expired_sessions_do_not_authenticate(accounts):
    _, token = accounts.bootstrap()
    with sqlite3.connect(accounts.path) as db:
        db.execute("UPDATE sessions SET expires_at=?", (time.time() - 1,))
    assert accounts.resolve(token) is None


def test_username_normalization_and_validation(accounts):
    first, _ = accounts.bootstrap()
    second, _ = accounts.bootstrap()
    accounts.upgrade(first["id"], "Ｐｏｅｔ", "first-password")
    with pytest.raises(AuthError) as exc:
        accounts.upgrade(second["id"], "poet", "second-password")
    assert exc.value.status == 409
    with pytest.raises(AuthError):
        accounts.upgrade(second["id"], "bad name", "second-password")
    with pytest.raises(AuthError):
        accounts.upgrade(second["id"], "goodname", "short")
    assert accounts.get(second["id"])["kind"] == "guest"


def test_admin_disable_and_enable_invalidates_sessions(accounts):
    admin = accounts.create_admin("admin", "admin-password")
    guest, token = accounts.bootstrap()
    with pytest.raises(AuthError) as exc:
        accounts.set_disabled(guest["id"], admin["id"], True)
    assert exc.value.status == 403
    accounts.set_disabled(admin["id"], guest["id"], True)
    assert accounts.resolve(token) is None
    with pytest.raises(AuthError) as exc:
        accounts.bootstrap(token)
    assert exc.value.status == 403
    accounts.set_disabled(admin["id"], guest["id"], False)
    assert accounts.resolve(token) is None
    with pytest.raises(AuthError):
        accounts.set_disabled(admin["id"], admin["id"], True)
    with sqlite3.connect(accounts.path) as db:
        rows = db.execute("SELECT action,result FROM admin_audit").fetchall()
    assert ("disable_user", "success") in rows
    assert ("disable_user", "failed") in rows


def test_reset_temporary_password_single_login_forces_change(accounts):
    admin = accounts.create_admin("admin", "admin-password")
    guest, _ = accounts.bootstrap()
    account, token = accounts.upgrade(guest["id"], "poet", "first-password")
    temp = accounts.reset_password(admin["id"], account["id"])
    assert accounts.resolve(token) is None
    with pytest.raises(AuthError):
        accounts.login("poet", "first-password")
    reset_user, reset_token = accounts.login("poet", temp)
    assert reset_user["must_change_password"]
    with pytest.raises(AuthError, match="临时密码已使用"):
        accounts.login("poet", temp)
    with pytest.raises(AuthError, match="不同的新密码"):
        accounts.change_credentials(account["id"], temp, username="新账号")
    with pytest.raises(AuthError, match="不同的新密码"):
        accounts.change_credentials(account["id"], temp, password=temp)
    changed, new_token = accounts.change_credentials(account["id"], temp, password="next-password")
    assert not changed["must_change_password"]
    assert accounts.resolve(reset_token) is None
    assert accounts.resolve(new_token)["id"] == account["id"]
    assert not accounts.login("poet", "next-password")[0]["must_change_password"]
    with sqlite3.connect(accounts.path) as db:
        raw = " ".join(str(tuple(r)) for r in db.execute("SELECT * FROM admin_audit"))
    assert temp not in raw


def test_guest_and_admin_password_reset_denied(accounts):
    admin = accounts.create_admin("admin", "admin-password")
    guest, _ = accounts.bootstrap()
    for target in (admin["id"], guest["id"]):
        with pytest.raises(AuthError):
            accounts.reset_password(admin["id"], target)
