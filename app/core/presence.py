"""
core/presence.py — Discord custom status reflecting the bot's current mood,
with an optional temporary "what it's doing" override (e.g. generating an
image) that takes priority over the mood text until cleared.

Kept separate from core/ai.py (which owns the mood value itself) because this
module needs to hold a reference to the discord.Client to call
change_presence() -- ai.py has no Discord dependency otherwise.
"""
import discord

_bot: discord.Client | None = None
_mood = "chill"
_activity: str | None = None


def bind(bot: discord.Client):
    global _bot
    _bot = bot


async def _apply():
    if _bot is None:
        return
    text = _activity or f"feeling {_mood}"
    try:
        await _bot.change_presence(activity=discord.CustomActivity(name=text))
    except discord.HTTPException:
        pass


async def set_mood(mood: str):
    global _mood
    _mood = mood
    if _activity is None:
        await _apply()


async def set_activity(text: str | None):
    """Push (text) or clear (None) a temporary status that overrides the mood
    text until cleared -- e.g. "drawing something" while /imagine runs."""
    global _activity
    _activity = text
    await _apply()
