import asyncio
import os
import tempfile
import discord
from core import ai
from core.ai import get_ai_response, maybe_shift_mood
from core.memory import load_memory, update_memory_from_conversation
from core.config import VOICE_MIN_UTTERANCE_MS, VOICE_IN_SAMPLE_RATE, VOICE_IN_CHANNELS
from . import dave_patch, stt, tts
from .audio import downsample_to_16k_mono
from .sink import UtteranceSink

# Must happen before any voice connection is made — see dave_patch's docstring.
dave_patch.apply()

# guild_id -> {"vc", "sink", "text_channel_id", "busy"}
sessions: dict[int, dict] = {}


def start_session(guild_id: int, vc: discord.VoiceClient, text_channel_id: int) -> UtteranceSink:
    loop = asyncio.get_running_loop()

    async def on_utterance(member: discord.Member, pcm: bytes):
        await handle_utterance(guild_id, member, pcm)

    sink = UtteranceSink(loop=loop, on_utterance=on_utterance, min_utterance_ms=VOICE_MIN_UTTERANCE_MS)
    vc.listen(sink)
    print(f"[voice_call] listening in guild {guild_id} (sink events: {len(sink.__sink_listeners__)})", flush=True)

    sessions[guild_id] = {
        "vc": vc,
        "sink": sink,
        "text_channel_id": text_channel_id,
        "busy": False,
    }
    return sink


def end_session(guild_id: int):
    session = sessions.pop(guild_id, None)
    if session and session["vc"].is_listening():
        session["vc"].stop_listening()


async def handle_utterance(guild_id: int, member: discord.Member, pcm_48k_stereo: bytes):
    session = sessions.get(guild_id)
    if not session or session["busy"]:
        return  # one utterance in flight at a time — drop overlapping speech

    session["busy"] = True
    session["sink"].paused = True
    try:
        pcm_16k = downsample_to_16k_mono(pcm_48k_stereo, VOICE_IN_SAMPLE_RATE, VOICE_IN_CHANNELS)
        transcript = await stt.transcribe(pcm_16k)
        if not transcript:
            print("[voice_call] no transcript — nothing recognized", flush=True)
            return
        print(f"[voice_call] heard: {transcript}", flush=True)

        channel_id = session["text_channel_id"]
        memory = load_memory()
        maybe_shift_mood()
        reply = get_ai_response(channel_id, transcript, member.display_name, member.id, memory)
        print(f"[voice_call] reply: {reply}", flush=True)
        if len(transcript.split()) > 5:
            update_memory_from_conversation(
                channel_id, str(member.id), member.display_name, memory, ai.histories
            )

        wav_bytes = await tts.synthesize(reply)
        if wav_bytes:
            print(f"[voice_call] speaking ({len(wav_bytes) / 1024:.1f}KB)", flush=True)
            await _play_reply(guild_id, wav_bytes)
    except Exception as e:
        print(f"[voice_call] utterance handling failed: {e}", flush=True)
    finally:
        if guild_id in sessions:
            sessions[guild_id]["busy"] = False
            sessions[guild_id]["sink"].paused = False


async def _play_reply(guild_id: int, wav_bytes: bytes):
    session = sessions.get(guild_id)
    if not session:
        return
    vc = session["vc"]

    fd, path = tempfile.mkstemp(suffix=".wav")
    with os.fdopen(fd, "wb") as f:
        f.write(wav_bytes)

    loop = asyncio.get_running_loop()
    done = asyncio.Event()

    def after(error):
        if error:
            print(f"[voice_call] playback error: {error}")
        try:
            os.remove(path)
        except Exception:
            pass
        loop.call_soon_threadsafe(done.set)

    vc.play(discord.FFmpegPCMAudio(path), after=after)
    await done.wait()
