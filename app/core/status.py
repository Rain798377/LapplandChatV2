"""
core/status.py — animated progress messages for long-running commands.

AnimatedStatus wraps a discord.Message and is a drop-in replacement anywhere
code already does `await status.edit(content="...")`: call sites don't need
to change, they just get a small spinner animated in front of the text while
the message is being updated.

It is self-expiring rather than relying on callers to clean it up: if no new
`.edit()` call arrives within `idle_timeout` seconds, the background task
stops itself and leaves the last text static. That matters here because most
commands using this have several early-return error paths, and forcing a
try/finally onto every one of them just to stop a spinner isn't worth it —
letting it time out on its own is simpler and just as clean from the user's
side (spinner freezes into plain text a few seconds after the last update).
"""

import time
import asyncio
import discord

_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


class AnimatedStatus:
    def __init__(self, message: discord.Message, interval: float = 0.55, idle_timeout: float = 6.0):
        self._message      = message
        self._interval     = interval
        self._idle_timeout = idle_timeout
        self._text         = message.content or ""
        self._frame_i       = 0
        self._stopped       = False
        self._last_touch    = time.monotonic()
        self._task          = asyncio.create_task(self._animate())

    async def _animate(self):
        try:
            while not self._stopped and time.monotonic() - self._last_touch <= self._idle_timeout:
                frame = _FRAMES[self._frame_i % len(_FRAMES)]
                self._frame_i += 1
                try:
                    await self._message.edit(content=f"-# {frame} {self._text}")
                except (discord.NotFound, discord.HTTPException):
                    return
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            return
        # Idle-expired rather than explicitly stopped/cancelled — leave clean, spinner-free text.
        try:
            await self._message.edit(content=self._text)
        except (discord.NotFound, discord.HTTPException):
            pass

    async def edit(self, *, content=None, **kwargs):
        """Mirrors discord.Message.edit(content=...). Extra kwargs (embed=,
        attachments=, view=, ...) mean this is a final state: stop animating
        and forward the edit untouched."""
        if kwargs:
            await self.stop()
            try:
                await self._message.edit(content=content, **kwargs)
            except (discord.NotFound, discord.HTTPException):
                pass
            return

        self._text       = content or ""
        self._last_touch = time.monotonic()
        if self._stopped:
            try:
                await self._message.edit(content=self._text)
            except (discord.NotFound, discord.HTTPException):
                pass

    async def stop(self, final_text: str | None = None):
        """Cancel the spinner and leave clean, spinner-free text behind
        (final_text if given, otherwise whatever the last .edit() set)."""
        self._stopped = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        try:
            await self._message.edit(content=final_text if final_text is not None else self._text)
        except (discord.NotFound, discord.HTTPException):
            pass

    def __getattr__(self, name):
        # Delegate anything not defined here (id, delete, channel, ...) to the underlying message.
        return getattr(self._message, name)
