import os
import json
from core.config import ALLOWED_CHANNELS_FILE, ALLOWED_CHANNELS as DEFAULT_ALLOWED_CHANNELS


def _load() -> list[int]:
    if os.path.exists(ALLOWED_CHANNELS_FILE):
        with open(ALLOWED_CHANNELS_FILE, "r") as f:
            return json.load(f)
    return list(DEFAULT_ALLOWED_CHANNELS)


def _save():
    os.makedirs(os.path.dirname(ALLOWED_CHANNELS_FILE), exist_ok=True)
    with open(ALLOWED_CHANNELS_FILE, "w") as f:
        json.dump(allowed_channels, f, indent=2)


# Mutated in place by add_channel/remove_channel — keep a single shared list
# object so anything that imported it directly (e.g. LapplandV2.py) sees updates.
allowed_channels: list[int] = _load()


def add_channel(channel_id: int) -> bool:
    """Add a channel to the listen list. Returns False if it was already there."""
    if channel_id in allowed_channels:
        return False
    allowed_channels.append(channel_id)
    _save()
    return True


def remove_channel(channel_id: int) -> bool:
    """Remove a channel from the listen list. Returns False if it wasn't there."""
    if channel_id not in allowed_channels:
        return False
    allowed_channels.remove(channel_id)
    _save()
    return True
