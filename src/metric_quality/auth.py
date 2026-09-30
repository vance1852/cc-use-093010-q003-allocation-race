"""为 HTTP 和命令行入口提供的角色权限层。"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


ROLES = {"operator", "engineer", "quality", "admin"}
PERMISSIONS = {
    "operator": {"read", "measure"},
    "engineer": {"read", "measure", "analyze", "submit"},
    "quality": {"read", "measure", "analyze", "approve", "release"},
    "admin": {"read", "measure", "analyze", "submit", "approve", "release", "admin"},
}


@dataclass(frozen=True)
class User:
    user_id: str
    role: str
    active: bool


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 80_000).hex()


class Auth:
    def __init__(self, connection: sqlite3.Connection):
        self.db = connection
        self.db.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id TEXT PRIMARY KEY, role TEXT NOT NULL, salt TEXT NOT NULL,
            password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS sessions(
            token TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1)""")
        self.db.commit()

    def create_user(self, user_id: str, password: str, role: str = "operator") -> User:
        if role not in ROLES or len(password) < 8:
            raise ValueError("invalid role or password")
        salt = secrets.token_hex(16)
        self.db.execute("INSERT INTO users VALUES(?,?,?,?,1,?)", (user_id, role, salt, _hash(password, salt), datetime.now(timezone.utc).isoformat()))
        self.db.commit()
        return User(user_id, role, True)

    def login(self, user_id: str, password: str) -> str:
        row = self.db.execute("SELECT role,salt,password_hash,active FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row or not row[3] or not hmac.compare_digest(_hash(password, row[1]), row[2]):
            raise PermissionError("invalid credentials")
        token = secrets.token_urlsafe(24)
        self.db.execute("INSERT INTO sessions VALUES(?,?,datetime('now','+8 hours'),1)", (token, user_id))
        self.db.commit()
        return token

    def current(self, token: str) -> User:
        row = self.db.execute("""SELECT u.user_id,u.role,u.active,s.active,s.expires_at
            FROM sessions s JOIN users u ON u.user_id=s.user_id
            WHERE s.token=?""", (token,)).fetchone()
        if not row or not row[2] or not row[3] or datetime.fromisoformat(row[4]).replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
            raise PermissionError("session expired")
        return User(row[0], row[1], True)

    def require(self, token: str, permission: str) -> User:
        user = self.current(token)
        if permission not in PERMISSIONS[user.role]:
            raise PermissionError("permission denied")
        return user

    def deactivate(self, user_id: str) -> None:
        self.db.execute("UPDATE users SET active=0 WHERE user_id=?", (user_id,))
        self.db.execute("UPDATE sessions SET active=0 WHERE user_id=?", (user_id,))
        self.db.commit()
