"""
bot_profile.py -- the bot's own presentation in the WebUI: an avatar photo,
a wide banner image, a short bio line (shown on the profile card popup),
and a longer "about" paragraph (shown in the chat's right-side info panel,
replacing the default computed one when set) -- all admin-editable (see
webui_server.py's /api/admin/bot-profile and WebUI/admin.html). Unlike a
user's own profile (photo/banner/desc), which lives in that browser's
localStorage since only they see it, the bot looks the same to everyone in
the room, so this has to be shared server-side -- same JSON-file pattern as
core/channels.py.

avatar/banner are stored as data URLs (like a user's own photo), not files
on disk -- avoids a multipart upload endpoint and a second image-serving
path alongside core/imagegen.py's, for assets this small (downscaled JPEGs,
same size class as a user's own photo already sitting in their browser's
localStorage).
"""

import os
import json
from core.config import BOT_PROFILE_FILE

_DEFAULT = {"avatar": "", "banner": "", "bio": "", "about": ""}


def load_bot_profile() -> dict:
    if os.path.exists(BOT_PROFILE_FILE):
        with open(BOT_PROFILE_FILE, "r") as f:
            return {**_DEFAULT, **json.load(f)}
    return dict(_DEFAULT)


def save_bot_profile(profile: dict) -> None:
    os.makedirs(os.path.dirname(BOT_PROFILE_FILE), exist_ok=True)
    with open(BOT_PROFILE_FILE, "w") as f:
        json.dump(profile, f, indent=2)
