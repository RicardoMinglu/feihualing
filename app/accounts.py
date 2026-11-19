"""Local identities, password credentials and opaque browser sessions.

Guest migration and credential changes use the same SQLite transaction as the
ownership changes. Neither a public user ID nor an old account cookie is a
session credential.
"""
from contextlib import contextmanager
import hashlib
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError


class AuthError(Exception):
    def __init__(self, message, status=400):
        self.message = message
        self.status = status
        super().__init__(message)


_PASSWORDS = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, type=Type.ID)
_HASH_SLOTS = threading.BoundedSemaphore(4)
_LEGACY_TOKEN = re.compile(r"[0-9a-f]{64}\Z")
_SESSION_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_DUMMY_HASH = None
_DUMMY_LOCK = threading.Lock()


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _password_hash(password):
    with _HASH_SLOTS:
        return _PASSWORDS.hash(password)


def _verify(encoded, password):
    if not isinstance(password, str) or len(password) > 128:
        return False
    with _HASH_SLOTS:
        try:
            return _PASSWORDS.verify(encoded, password)
        except (VerificationError, InvalidHashError):
            return False


def _check_password(password):
    if not isinstance(password, str) or not 8 <= len(password) <= 128:
        raise AuthError("密码需为 8 至 128 个字符")
    if not password.strip():
        raise AuthError("密码不能全为空格")


def _check_username(username):
    if not isinstance(username, str):
        raise AuthError("请输入账号名")
    name = unicodedata.normalize("NFKC", username).strip()
    if not 2 <= len(name) <= 32 or not all(c.isalnum() or c in "_-" for c in name):
        raise AuthError("账号名需为 2 至 32 个字母、数字、汉字、下划线或短横线")
    return name, name.casefold()


class Accounts:
    def __init__(self, path):
        global _DUMMY_HASH
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _DUMMY_LOCK:
            if _DUMMY_HASH is None:
                _DUMMY_HASH = _password_hash(secrets.token_urlsafe(32))
        with self.tx() as db:
            self._schema(db)
            self._migrate_legacy(db)

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            # Concurrent first opens may bypass SQLite's busy timeout while
            # upgrading the journal lock. Retry only before a transaction starts.
            deadline = time.monotonic() + 15
            while True:
                try:
                    db.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if exc.sqlite_errorcode != sqlite3.SQLITE_BUSY or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _schema(db):
        statements = [
            """CREATE TABLE IF NOT EXISTS users(
                id TEXT PRIMARY KEY, username TEXT, username_key TEXT UNIQUE,
                password_hash TEXT, kind TEXT NOT NULL CHECK(kind IN ('guest','account')),
                role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user','admin')),
                disabled INTEGER NOT NULL DEFAULT 0,
                must_change_password INTEGER NOT NULL DEFAULT 0,
                temporary_password_used INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, last_active_at REAL NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS sessions(
                token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                created_at REAL NOT NULL, expires_at REAL NOT NULL, revoked_at REAL)""",
            "CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id)",
            """CREATE TABLE IF NOT EXISTS legacy_identities(
                legacy_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                created_at REAL NOT NULL, revoked_at REAL)""",
            """CREATE TABLE IF NOT EXISTS admin_audit(
                id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, target_id TEXT NOT NULL,
                action TEXT NOT NULL, result TEXT NOT NULL, created_at REAL NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS users_active ON users(last_active_at DESC)",
            "CREATE INDEX IF NOT EXISTS admin_audit_time ON admin_audit(created_at DESC)",
            "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY)",
        ]
        for statement in statements:
            db.execute(statement)

    @staticmethod
    def _has_table(db, name):
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    def _migrate_legacy(self, db):
        """Map only actual legacy records; an arbitrary cookie never creates a map."""
        tables = [name for name in ("games", "game_summaries", "visitor_profiles") if self._has_table(db, name)]
        owners = set()
        for table in tables:
            owners.update(row[0] for row in db.execute(f"SELECT DISTINCT owner FROM {table}"))
        migrated = 0
        now = time.time()
        for owner in sorted(o for o in owners if isinstance(o, str) and _LEGACY_TOKEN.fullmatch(o)):
            legacy_hash = _hash(owner)
            mapping = db.execute("SELECT user_id FROM legacy_identities WHERE legacy_hash=?", (legacy_hash,)).fetchone()
            if mapping:
                user_id = mapping[0]
            else:
                created = now
                last_active = 0.0
                if "game_summaries" in tables:
                    row = db.execute("SELECT min(created_at),max(coalesce(ended_at,created_at)) FROM game_summaries WHERE owner=?", (owner,)).fetchone()
                    if row[0] is not None:
                        created = min(created, row[0])
                        last_active = max(last_active, row[1] or 0)
                if "visitor_profiles" in tables:
                    row = db.execute("SELECT created_at,updated_at FROM visitor_profiles WHERE owner=?", (owner,)).fetchone()
                    if row:
                        created = min(created, row[0])
                        last_active = max(last_active, row[1])
                user_id = self._new_user(db, created_at=created, last_active_at=last_active)
                db.execute("INSERT INTO legacy_identities VALUES(?,?,?,NULL)", (legacy_hash, user_id, now))
                migrated += 1
            for table in tables:
                db.execute(f"UPDATE {table} SET owner=? WHERE owner=?", (user_id, owner))
        db.execute("INSERT OR IGNORE INTO schema_migrations VALUES(2)")
        return migrated

    @staticmethod
    def _new_user(db, created_at=None, last_active_at=None):
        now = time.time() if created_at is None else created_at
        uid = "u_" + secrets.token_hex(16)
        db.execute("INSERT INTO users(id,kind,created_at,last_active_at) VALUES(?,'guest',?,?)", (uid, now, now if last_active_at is None else last_active_at))
        return uid

    def _public(self, db, row):
        if row is None:
            return None
        has_records = self._has_table(db, "games") and db.execute("SELECT 1 FROM games WHERE owner=? LIMIT 1", (row["id"],)).fetchone() is not None
        return {
            "id": row["id"], "username": row["username"],
            "display_name": row["username"] or "游客·" + row["id"][-6:],
            "kind": row["kind"], "role": row["role"], "disabled": bool(row["disabled"]),
            "must_change_password": bool(row["must_change_password"]),
            "created_at": row["created_at"], "last_active_at": row["last_active_at"],
            "has_records": bool(has_records),
        }

    @staticmethod
    def _session(db, uid):
        token = secrets.token_urlsafe(32)
        now = time.time()
        kind = db.execute("SELECT kind FROM users WHERE id=?", (uid,)).fetchone()[0]
        lifetime = (180 if kind == "guest" else 30) * 86400
        db.execute("INSERT INTO sessions VALUES(?,?,?,?,NULL)", (_hash(token), uid, now, now + lifetime))
        return token

    @staticmethod
    def _revoke(db, uid):
        db.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (time.time(), uid))

    def _user(self, db, uid):
        row = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if row is None:
            raise AuthError("用户不存在", 404)
        if row["disabled"]:
            raise AuthError("此用户身份已被停用", 403)
        return row

    def bootstrap(self, session_token=None, legacy_token=None):
        with self.tx() as db:
            if isinstance(session_token, str) and _SESSION_TOKEN.fullmatch(session_token):
                row = db.execute("""SELECT u.*,s.expires_at,s.revoked_at FROM sessions s
                    JOIN users u ON u.id=s.user_id WHERE s.token_hash=?""", (_hash(session_token),)).fetchone()
                if row and row["disabled"]:
                    raise AuthError("此用户身份已被停用", 403)
                if row and row["revoked_at"] is None and row["expires_at"] > time.time():
                    return self._public(db, row), None
            if isinstance(legacy_token, str) and _LEGACY_TOKEN.fullmatch(legacy_token):
                row = db.execute("""SELECT u.* FROM legacy_identities l JOIN users u ON u.id=l.user_id
                    WHERE l.legacy_hash=? AND l.revoked_at IS NULL AND u.kind='guest'""", (_hash(legacy_token),)).fetchone()
                if row:
                    if row["disabled"]:
                        raise AuthError("此用户身份已被停用", 403)
                    return self._public(db, row), self._session(db, row["id"])
            uid = self._new_user(db)
            return self._public(db, self._user(db, uid)), self._session(db, uid)

    def resolve(self, token):
        if not isinstance(token, str) or not _SESSION_TOKEN.fullmatch(token):
            return None
        with sqlite3.connect(self.path, timeout=15) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("""SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
                WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>?
                AND u.disabled=0""", (_hash(token), time.time())).fetchone()
            return self._public(db, row)

    def get(self, user_id):
        with sqlite3.connect(self.path, timeout=15) as db:
            db.row_factory = sqlite3.Row
            return self._public(db, db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())

    def upgrade(self, user_id, username, password):
        name, key = _check_username(username)
        _check_password(password)
        encoded = _password_hash(password)
        with self.tx() as db:
            row = self._user(db, user_id)
            if row["kind"] != "guest":
                raise AuthError("此用户已设置账号，请使用修改账号功能", 409)
            if db.execute("SELECT 1 FROM users WHERE username_key=?", (key,)).fetchone():
                raise AuthError("账号名已被使用", 409)
            db.execute("UPDATE users SET username=?,username_key=?,password_hash=?,kind='account' WHERE id=?", (name, key, encoded, user_id))
            db.execute("UPDATE legacy_identities SET revoked_at=? WHERE user_id=?", (time.time(), user_id))
            self._revoke(db, user_id)
            token = self._session(db, user_id)
            return self._public(db, self._user(db, user_id)), token

    def login(self, username, password):
        try:
            _, key = _check_username(username)
        except AuthError:
            _verify(_DUMMY_HASH, password)
            raise AuthError("账号名或密码错误", 401)
        # Keep the relatively expensive hash operation outside the SQLite write lock.
        with sqlite3.connect(self.path, timeout=15) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM users WHERE username_key=?", (key,)).fetchone()
        encoded = row["password_hash"] if row else _DUMMY_HASH
        valid = _verify(encoded, password)
        if not row or not valid:
            raise AuthError("账号名或密码错误", 401)
        with self.tx() as db:
            current = self._user(db, row["id"])
            # A concurrent credential change/reset invalidates the checked hash.
            if current["password_hash"] != encoded or current["username_key"] != key:
                raise AuthError("账号名或密码错误", 401)
            if current["must_change_password"]:
                if current["temporary_password_used"]:
                    raise AuthError("临时密码已使用，请联系管理员重新设置", 401)
                db.execute("UPDATE users SET temporary_password_used=1 WHERE id=?", (current["id"],))
                self._revoke(db, current["id"])
            token = self._session(db, current["id"])
            return self._public(db, current), token

    def change_credentials(self, user_id, current_password, username=None, password=None):
        name_key = _check_username(username) if username is not None else None
        if password is not None:
            _check_password(password)
        if name_key is None and password is None:
            raise AuthError("请输入新的账号名或密码")
        with sqlite3.connect(self.path, timeout=15) as db:
            db.row_factory = sqlite3.Row
            row = self._user(db, user_id)
        if row["kind"] != "account" or not _verify(row["password_hash"], current_password):
            raise AuthError("当前密码错误", 403)
        if row["must_change_password"] and (password is None or password == current_password):
            raise AuthError("请设置与临时密码不同的新密码")
        encoded = _password_hash(password) if password is not None else row["password_hash"]
        with self.tx() as db:
            current = self._user(db, user_id)
            if current["password_hash"] != row["password_hash"]:
                raise AuthError("凭据已发生变化，请重新登录", 401)
            name, key = name_key or (current["username"], current["username_key"])
            if db.execute("SELECT 1 FROM users WHERE username_key=? AND id<>?", (key, user_id)).fetchone():
                raise AuthError("账号名已被使用", 409)
            db.execute("""UPDATE users SET username=?,username_key=?,password_hash=?,
                must_change_password=0,temporary_password_used=0 WHERE id=?""", (name, key, encoded, user_id))
            self._revoke(db, user_id)
            token = self._session(db, user_id)
            return self._public(db, self._user(db, user_id)), token

    def logout(self, token):
        if not isinstance(token, str) or not _SESSION_TOKEN.fullmatch(token):
            return
        with self.tx() as db:
            db.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL", (time.time(), _hash(token)))

    def _admin(self, db, actor_id):
        actor = self._user(db, actor_id)
        if actor["role"] != "admin" or actor["must_change_password"]:
            raise AuthError("需要管理员权限", 403)

    @staticmethod
    def _audit(db, actor_id, target_id, action, result):
        db.execute("INSERT INTO admin_audit VALUES(?,?,?,?,?,?)", ("a_" + secrets.token_hex(12), actor_id, target_id, action, result, time.time()))

    def audit(self, actor_id, target_id, action, result="success"):
        with self.tx() as db:
            self._audit(db, actor_id, target_id, action, result)

    def set_disabled(self, actor_id, target_id, disabled):
        action = "disable_user" if disabled else "enable_user"
        try:
            with self.tx() as db:
                self._admin(db, actor_id)
                target = db.execute("SELECT * FROM users WHERE id=?", (target_id,)).fetchone()
                if not target:
                    raise AuthError("用户不存在", 404)
                if target["role"] != "user":
                    raise AuthError("此页面不能停用管理员", 403)
                db.execute("UPDATE users SET disabled=? WHERE id=?", (int(bool(disabled)), target_id))
                if disabled:
                    self._revoke(db, target_id)
                self._audit(db, actor_id, target_id, action, "success")
        except AuthError:
            self.audit(actor_id, target_id, action, "failed")
            raise
        return self.get(target_id)

    def reset_password(self, actor_id, target_id):
        temporary = secrets.token_urlsafe(15)
        encoded = _password_hash(temporary)
        try:
            with self.tx() as db:
                self._admin(db, actor_id)
                target = db.execute("SELECT * FROM users WHERE id=?", (target_id,)).fetchone()
                if not target:
                    raise AuthError("用户不存在", 404)
                if target["role"] != "user" or target["kind"] != "account":
                    raise AuthError("只能重置普通正式账号的密码", 403)
                db.execute("UPDATE users SET password_hash=?,must_change_password=1,temporary_password_used=0 WHERE id=?", (encoded, target_id))
                self._revoke(db, target_id)
                self._audit(db, actor_id, target_id, "reset_password", "success")
        except AuthError:
            self.audit(actor_id, target_id, "reset_password", "failed")
            raise
        return temporary

    def create_admin(self, username, password):
        name, key = _check_username(username)
        _check_password(password)
        encoded = _password_hash(password)
        with self.tx() as db:
            if db.execute("SELECT 1 FROM users WHERE username_key=?", (key,)).fetchone():
                raise AuthError("账号名已被使用", 409)
            uid = self._new_user(db)
            db.execute("UPDATE users SET username=?,username_key=?,password_hash=?,kind='account',role='admin' WHERE id=?", (name, key, encoded, uid))
            self._audit(db, "server-cli", uid, "create_admin", "success")
            return self._public(db, self._user(db, uid))
