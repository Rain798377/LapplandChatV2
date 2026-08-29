"""
gate.py -- signs/verifies the WebUI's hidden root-page gate cookie (see
webui_server.py's GET p("/") and POST p("/gate"), WebUI/cover.html, and
core/config.py's WEBUI_GATE_* note on why this is obscurity, not real auth).

Stateless: the token is just a random nonce plus an HMAC over it, so
validity is checked by recomputing the HMAC, not by looking anything up in
a database or session table -- there's no per-user identity to track here,
only "did whoever's holding this cookie once know the passphrase."
"""

import hashlib
import hmac
import secrets

from core.config import WEBUI_GATE_SECRET


def issue_token() -> str:
    nonce = secrets.token_urlsafe(16)
    mac = hmac.new(WEBUI_GATE_SECRET.encode(), nonce.encode(), hashlib.sha256).hexdigest()
    return f"{nonce}.{mac}"


def is_valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    nonce, _, mac = token.rpartition(".")
    expected = hmac.new(WEBUI_GATE_SECRET.encode(), nonce.encode(), hashlib.sha256).hexdigest()
    # constant-time compare -- same reasoning as core/auth.py's password
    # check: a plain == leaks the matching prefix length through timing
    return hmac.compare_digest(mac, expected)
