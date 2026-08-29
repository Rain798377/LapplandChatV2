"""
chat_store.py — server-side persistence for the web UI's channels
(WebUI/index.html). Every channel is now genuinely shared: everyone who
opens #general (or any other channel) sees the same message stream,
Discord-style, instead of the old per-browser localStorage-only history
nobody else could see. Only WEBUI_BOT_CHANNEL also gets bot replies (see
core/config.py); the rest are shared note-taking with no LLM call, but
still a shared table now, not a private one.

Lives in the same SQLite file core/auth.py uses (WEBUI_DB_FILE) -- one file
for the web UI's whole server-side state. Same connection-per-call pattern
as core/auth.py, for the same reason (sqlite3 connections aren't safe to
share across threads; every call here is run via asyncio.to_thread from
webui_server.py).

webui_server.py's frontend-facing polling loop (WebUI/index.html) is what
actually makes this feel live: there's no push/WebSocket here, just
GET /api/messages/{channel}?since=<id> called every few seconds. Fine for a
10-20 person group; a real push channel would be the next step if this ever
needed to feel more instant than a few seconds of latency.
"""

import os
import sqlite3
from datetime import datetime, timezone

from core.config import WEBUI_DB_FILE


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(WEBUI_DB_FILE), exist_ok=True)
    conn = sqlite3.connect(WEBUI_DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                channel    TEXT NOT NULL,
                is_bot     INTEGER NOT NULL,
                user_id    TEXT,
                username   TEXT NOT NULL,
                body       TEXT NOT NULL,
                image      TEXT,
                stats      TEXT,
                created_at TEXT NOT NULL,
                edited     INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Older DBs (pre-edit/delete) won't have this column -- CREATE TABLE
        # IF NOT EXISTS above only applies to brand-new files. Ignore the
        # "duplicate column" error on every DB that already has it.
        try:
            conn.execute("ALTER TABLE messages ADD COLUMN edited INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        # Every read here filters by channel and orders/paginates by id --
        # this is the one index that matters.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_channel_id ON messages (channel, id)")
        conn.commit()
    finally:
        conn.close()


def add_message(
    channel: str,
    is_bot: bool,
    username: str,
    body: str,
    user_id: str | None = None,
    image: str | None = None,
    stats: str | None = None,
) -> sqlite3.Row:
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO messages (channel, is_bot, user_id, username, body, image, stats, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (channel, int(is_bot), user_id, username, body, image, stats, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return conn.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()
    finally:
        conn.close()


def get_messages(channel: str, since_id: int = 0, limit: int = 200) -> list[sqlite3.Row]:
    """since_id=0 -- the most recent `limit` messages (oldest first).
    since_id>0 -- everything newer than that id (for polling), oldest first,
    capped at `limit` so one call can't return an unbounded backlog."""
    conn = _connect()
    try:
        if since_id:
            return conn.execute(
                "SELECT * FROM messages WHERE channel = ? AND id > ? ORDER BY id ASC LIMIT ?",
                (channel, since_id, limit),
            ).fetchall()
        rows = conn.execute(
            "SELECT * FROM messages WHERE channel = ? ORDER BY id DESC LIMIT ?",
            (channel, limit),
        ).fetchall()
        return list(reversed(rows))
    finally:
        conn.close()


def message_counts() -> dict[str, int]:
    """channel -> total message count, for the admin panel's channel list
    (webui_server.py's /api/admin/overview)."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT channel, COUNT(*) AS n FROM messages GROUP BY channel").fetchall()
        return {row["channel"]: row["n"] for row in rows}
    finally:
        conn.close()


def clear_channel(channel: str) -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM messages WHERE channel = ?", (channel,))
        conn.commit()
    finally:
        conn.close()


def get_message(message_id: int) -> sqlite3.Row | None:
    conn = _connect()
    try:
        return conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    finally:
        conn.close()


def edit_message(
    message_id: int, body: str, image: str | None = None, stats: str | None = None, mark_edited: bool = True,
) -> sqlite3.Row | None:
    """image/stats are only written when explicitly passed -- omitting them
    (the default) leaves whatever's already stored alone. mark_edited=True
    (the default) is for a real user/admin edit via /api/messages/{id}/edit;
    webui_server.py's /imagine progress updates pass mark_edited=False since
    that's the bot filling in its own not-yet-finished message, not
    something a person edited, so it shouldn't pick up an "(edited)" tag."""
    fields = ["body = ?"]
    params: list = [body]
    if image is not None:
        fields.append("image = ?")
        params.append(image)
    if stats is not None:
        fields.append("stats = ?")
        params.append(stats)
    if mark_edited:
        fields.append("edited = 1")
    params.append(message_id)

    conn = _connect()
    try:
        conn.execute(f"UPDATE messages SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()
        return conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    finally:
        conn.close()


def delete_message(message_id: int) -> bool:
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def serialize(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "author": row["username"],
        "bot": bool(row["is_bot"]),
        "userId": row["user_id"],
        "body": row["body"],
        "image": row["image"],
        "stats": row["stats"],
        "createdAt": row["created_at"],
        "edited": bool(row["edited"]),
    }
