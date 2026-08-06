import aiohttp
from core.config import STT_SERVER_URL
from .audio import pcm_to_wav_bytes


async def transcribe(pcm_16k_mono: bytes) -> str:
    """POST 16kHz mono PCM to the local whisper.cpp sidecar, return the transcript text."""
    wav_bytes = pcm_to_wav_bytes(pcm_16k_mono, sample_rate=16000, channels=1)
    try:
        form = aiohttp.FormData()
        form.add_field("file", wav_bytes, filename="utterance.wav", content_type="audio/wav")
        form.add_field("response_format", "json")
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{STT_SERVER_URL}/inference",
                data=form,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    print(f"[stt] server returned {resp.status}")
                    return ""
                data = await resp.json()
        return (data.get("text") or "").strip()
    except Exception as e:
        print(f"[stt] transcription failed: {e}")
        return ""
