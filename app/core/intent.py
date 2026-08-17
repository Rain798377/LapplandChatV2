"""
intent.py — client for the optional local intent-classification sidecar (see
intent_server/README.md at the repo root for the server side).

classify_intent() asks the sidecar whether a message is directed at the bot.
It never raises and never blocks the bot on the sidecar being present --
any failure (unset config, sidecar off/unreachable, an uncertain verdict)
just returns None, and on_message (LapplandV2.py) falls back to its existing
reply-chance heuristics unchanged. This mirrors core/gpu_worker.py's
never-raises, always-has-a-fallback shape.
"""

import time

import requests

from core.colors import *
from core.config import (
    INTENT_SERVER_URL,
    INTENT_SERVER_API_KEY,
    INTENT_CONNECT_TIMEOUT,
    INTENT_RETRY_COOLDOWN,
)

_unavailable_until = 0.0


def classify_intent(text: str) -> bool | None:
    """Ask the sidecar whether `text` is directed at the bot. Returns True/
    False on a confident verdict, or None if the sidecar is unset,
    unreachable, or itself uncertain -- callers should treat None as "no
    opinion" and fall back to their own heuristics."""
    global _unavailable_until

    if not INTENT_SERVER_URL:
        return None
    if time.time() < _unavailable_until:
        return None

    try:
        response = requests.post(
            f"{INTENT_SERVER_URL.rstrip('/')}/classify",
            json={"text": text},
            headers={"X-Api-Key": INTENT_SERVER_API_KEY} if INTENT_SERVER_API_KEY else {},
            timeout=(INTENT_CONNECT_TIMEOUT, 3),
        )
    except requests.exceptions.RequestException as e:
        _unavailable_until = time.time() + INTENT_RETRY_COOLDOWN
        print(f"{YELLOW}[intent] sidecar unreachable ({e.__class__.__name__}), "
              f"falling back to heuristics for {INTENT_RETRY_COOLDOWN}s{RESET}", flush=True)
        return None

    if response.status_code != 200:
        return None

    return response.json().get("directed_at_bot")
