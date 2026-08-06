import asyncio
import aiohttp
from core.config import TTS_SERVER_URL, TTS_SERVER_TOKEN

# The remote TTS server is CPU-limited on its end too — only one request in flight at a time.
_tts_lock = asyncio.Lock()


async def synthesize(text: str) -> bytes | None:
    """POST reply text to the remote TTS server, return raw WAV bytes (or None on failure)."""
    async with _tts_lock:
        try:
            headers = {"Authorization": f"Bearer {TTS_SERVER_TOKEN}"}
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    TTS_SERVER_URL,
                    headers=headers,
                    json={"text": text},
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status != 200:
                        print(f"[tts] server returned {resp.status}")
                        return None
                    return await resp.read()
        except Exception as e:
            print(f"[tts] synthesis failed: {e}")
            return None
