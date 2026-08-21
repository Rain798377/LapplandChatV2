"""
auth.py — accounts for the web UI's login page (WebUI/login.html).

SQLite-backed (stdlib sqlite3, no new dependency): a `users` table (user_id,
username, email, password_hash, created_at) and a `sessions` table (token ->
user_id) backing webui_server.py's /api/register, /api/login, /api/logout,
/api/me. A fresh connection is opened per call rather than held open --
sqlite3 connections aren't safe to share across threads, and FastAPI runs
these off the event loop via asyncio.to_thread, same as every other blocking
call in this app (see webui_server.py).

Passwords are hashed with PBKDF2-HMAC-SHA256 (hashlib, stdlib) at 260,000
iterations -- OWASP's 2023 minimum for that algorithm -- rather than pulling
in bcrypt/argon2 for one extra dependency. Stored as
"pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>" so the iteration count and
algorithm travel with the hash (lets a future bump to the count re-hash on
next login instead of invalidating every password at once, though nothing
here does that yet).
"""

import hashlib
import os
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone

from core.config import AUTH_DB_FILE

_HASH_ALGO = "pbkdf2_sha256"
_HASH_ITERATIONS = 260_000


class UsernameTakenError(ValueError):
    """Raised by create_user() when the username is already registered."""


class EmailTakenError(ValueError):
    """Raised by create_user() when the email is already registered."""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(AUTH_DB_FILE), exist_ok=True)
    conn = sqlite3.connect(AUTH_DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id       TEXT PRIMARY KEY,
                username      TEXT UNIQUE NOT NULL COLLATE NOCASE,
                email         TEXT UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at    TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token      TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL REFERENCES users(user_id),
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()
    finally:
        conn.close()


# ── passwords ────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _HASH_ITERATIONS).hex()
    return f"{_HASH_ALGO}${_HASH_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, digest = stored.split("$")
        if algo != _HASH_ALGO:
            return False
        check = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
        # constant-time compare -- a plain == leaks the matching prefix
        # length through response timing
        return secrets.compare_digest(check, digest)
    except (ValueError, AttributeError):
        return False


# ── users ────────────────────────────────────────────────────────────────

def _username_from_email(conn: sqlite3.Connection, email: str) -> str:
    """login.html sends only `email` (no `username`) when someone registers
    with an email address in the single identifier field -- derive a
    reasonable username from it rather than storing a nameless account,
    since the whole point of this table is to have a real username on file."""
    base = re.sub(r"[^a-zA-Z0-9_]", "", email.split("@", 1)[0]) or "user"
    username = base
    suffix = 0
    while conn.execute("SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone():
        suffix += 1
        username = f"{base}{suffix}"
    return username


def create_user(username: str | None, password: str, email: str | None = None) -> tuple[str, str]:
    """Returns (user_id, username) -- username is echoed back since it may
    have been derived from the email (see _username_from_email)."""
    conn = _connect()
    try:
        username = (username or "").strip() or None
        email = (email or "").strip() or None
        if not username:
            if not email:
                raise ValueError("username or email is required")
            username = _username_from_email(conn, email)

        user_id = uuid.uuid4().hex
        password_hash = hash_password(password)
        try:
            conn.execute(
                "INSERT INTO users (user_id, username, email, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, username, email, password_hash, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        except sqlite3.IntegrityError as e:
            msg = str(e)
            if "users.username" in msg:
                raise UsernameTakenError(username) from e
            if "users.email" in msg:
                raise EmailTakenError(email) from e
            raise
        return user_id, username
    finally:
        conn.close()


def get_user_by_identifier(identifier: str) -> sqlite3.Row | None:
    conn = _connect()
    try:
        return conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE OR email = ? COLLATE NOCASE",
            (identifier, identifier),
        ).fetchone()
    finally:
        conn.close()


# ── sessions ─────────────────────────────────────────────────────────────

def create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
            (token, user_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def get_user_by_session(token: str) -> sqlite3.Row | None:
    conn = _connect()
    try:
        return conn.execute(
            """SELECT users.* FROM sessions
               JOIN users ON users.user_id = sessions.user_id
               WHERE sessions.token = ?""",
            (token,),
        ).fetchone()
    finally:
        conn.close()


def delete_session(token: str) -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()
