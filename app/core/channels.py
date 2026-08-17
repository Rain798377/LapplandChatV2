import os
import json
from core.config import (
    ALLOWED_CHANNELS_FILE, ALLOWED_CHANNELS as DEFAULT_ALLOWED_CHANNELS,
    IDLE_CHANNELS_FILE, IDLE_CHANNELS as DEFAULT_IDLE_CHANNELS,
)


def _load(path: str, default: list[int]) -> list[int]:
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return list(default)


def _save(path: str, channel_ids: list[int]):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(channel_ids, f, indent=2)


# Mutated in place by add_channel/remove_channel — keep a single shared list
# object so anything that imported it directly (e.g. LapplandV2.py) sees updates.
allowed_channels: list[int] = _load(ALLOWED_CHANNELS_FILE, DEFAULT_ALLOWED_CHANNELS)
# Same pattern, separate list: channels eligible for unprompted idle chatter
# (see LapplandV2.py's idle_chatter task). Independent of allowed_channels --
# a channel doesn't need to be in the normal listen list to get idle lines.
idle_channels: list[int] = _load(IDLE_CHANNELS_FILE, DEFAULT_IDLE_CHANNELS)


def add_channel(channel_id: int) -> bool:
    """Add a channel to the listen list. Returns False if it was already there."""
    if channel_id in allowed_channels:
        return False
    allowed_channels.append(channel_id)
    _save(ALLOWED_CHANNELS_FILE, allowed_channels)
    return True


def remove_channel(channel_id: int) -> bool:
    """Remove a channel from the listen list. Returns False if it wasn't there."""
    if channel_id not in allowed_channels:
        return False
    allowed_channels.remove(channel_id)
    _save(ALLOWED_CHANNELS_FILE, allowed_channels)
    return True


def add_idle_channel(channel_id: int) -> bool:
    """Add a channel to the idle-chatter list. Returns False if already there."""
    if channel_id in idle_channels:
        return False
    idle_channels.append(channel_id)
    _save(IDLE_CHANNELS_FILE, idle_channels)
    return True


def remove_idle_channel(channel_id: int) -> bool:
    """Remove a channel from the idle-chatter list. Returns False if it wasn't there."""
    if channel_id not in idle_channels:
        return False
    idle_channels.remove(channel_id)
    _save(IDLE_CHANNELS_FILE, idle_channels)
    return True
