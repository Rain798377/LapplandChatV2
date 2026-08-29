"""
discord_notify.py -- relays WebUI messages into Discord via a channel webhook.

This deployment runs independently of the Discord bot process (see
core/config.py's load_dotenv comment), so there's no bot gateway connection
to send a DM through. A Discord webhook is just an HTTP POST -- no bot
process required on this end -- so that's what notify() uses to post into
whatever Discord channel DISCORD_NOTIFY_WEBHOOK_URL points at (e.g. a
private channel only BOT_OWNER_ID reads).
"""

import aiohttp

from core.colors import RED, RESET
from core.config import DISCORD_NOTIFY_WEBHOOK_URL


async def notify(channel: str, username: str, message: str) -> None:
    """Post a WebUI message into Discord. No-op if no webhook is configured."""
    if not DISCORD_NOTIFY_WEBHOOK_URL:
        return
    content = f"**[{channel}]** {username}: {message}"[:2000]  # Discord's hard cap on webhook content
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(DISCORD_NOTIFY_WEBHOOK_URL, json={"content": content}) as resp:
                if resp.status >= 300:
                    body = await resp.text()
                    print(f"{RED}[discord_notify] webhook returned {resp.status}: {body}{RESET}", flush=True)
    except Exception as e:
        print(f"{RED}[discord_notify] failed to post: {e}{RESET}", flush=True)
