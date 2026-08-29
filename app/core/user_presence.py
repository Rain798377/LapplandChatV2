"""
user_presence.py -- "who's online" tracking for the WebUI's right-panel
member list (see webui_server.py's /api/users, WebUI/chat.js's
renderMembersList). In-memory only, not persisted -- resets on restart,
which is fine for presence; nobody expects "online" status to survive one.

Online is inferred from recent API activity rather than a dedicated
heartbeat endpoint: WebUI/chat.js already polls /api/messages/{channel}
every few seconds (POLL_MS) for as long as a channel is open, and every
authenticated request already runs through webui_server.py's require_user
-- touch() is called there, so this piggybacks on traffic that already
exists instead of adding a separate ping.

Not the same thing as core/presence.py, which is the Discord bot's own
custom-status text (unrelated -- that's about what the bot displays on
Discord, this is about which WebUI accounts are currently active).
"""

import time

# Generous relative to chat.js's 3s poll interval, so one slow/missed tick
# doesn't flicker someone's status between online and offline.
ONLINE_THRESHOLD_SECONDS = 20

_last_seen: dict[str, float] = {}


def touch(user_id: str) -> None:
    _last_seen[user_id] = time.time()


def is_online(user_id: str) -> bool:
    return (time.time() - _last_seen.get(user_id, 0.0)) < ONLINE_THRESHOLD_SECONDS
