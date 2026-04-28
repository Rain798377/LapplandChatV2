import os
import re
import asyncio
import random
import discord
from config import AUTOPLAY_DELAY, MAX_FILE_SIZE_MB
from .utils import _apply_loudnorm
from .audio import search_and_download_audio
from .embed import build_now_playing_embed


FFMPEG_OPTIONS = {
    "options": "-vn",
}

AUTOPLAY_QUERIES = [
    "ytsearch1:{title} official audio",
    "ytsearch1:songs like {title}",
    "ytsearch1:{title} similar songs mix",
    "ytsearch1:{title} type beat",
]

# guild_id → state dict
# state keys: queue, current_file, current_label, current_meta, last_title,
#             volume, autoplaying, autoplay_task, text_channel_id, normalize
voice_states = {}


# ── Autoplay ──────────────────────────────────────────────────────────────────

def _cancel_autoplay(guild_id: int):
    state = voice_states.get(guild_id)
    if state and state.get("autoplay_task"):
        state["autoplay_task"].cancel()
        state["autoplay_task"] = None


async def _schedule_autoplay_task(guild_id: int, vc: discord.VoiceClient, bot: discord.Client):
    """
    Runs on the bot's event loop. Creates a real asyncio.Task for autoplay so that
    _cancel_autoplay can properly inject CancelledError into a sleeping coroutine.
    """
    state = voice_states.get(guild_id)
    if not state:
        return
    task = asyncio.ensure_future(_autoplay_after_delay(guild_id, vc, bot))
    state["autoplay_task"] = task
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _autoplay_after_delay(guild_id: int, vc: discord.VoiceClient, bot: discord.Client):
    try:
        state = voice_states.get(guild_id)
        if state and state.pop("skip_autoplay_delay", False):
            pass  # skip the delay
        else:
            await asyncio.sleep(AUTOPLAY_DELAY)

        state = voice_states.get(guild_id)
        if not state or vc.is_playing() or vc.is_paused():
            return
        if not vc.is_connected():
            return
        if not any(not m.bot for m in vc.channel.members):
            return

        last_title = state.get("last_title") or "popular music"
        clean = re.sub(r"\(.*?\)|\[.*?\]", "", last_title).strip()
        clean = re.split(r"\s*[-|]\s*", clean)[0].strip()

        search_query = random.choice(AUTOPLAY_QUERIES).format(title=clean)

        filepath, meta = await search_and_download_audio(search_query)
        if not filepath:
            return

        resolved_title = meta.get("title") if meta else None
        if resolved_title and resolved_title.lower() == (state.get("last_title") or "").lower():
            return

        state["current_file"]  = filepath
        state["current_label"] = resolved_title
        state["current_meta"]  = meta
        state["autoplaying"]   = True
        state["last_title"]    = resolved_title

        vol = state.get("volume", 1.0)
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(filepath, **FFMPEG_OPTIONS),
            volume=vol
        )

        def after(error):
            if error:
                print(f"[autoplay error] {error}")
            play_next(guild_id, vc, bot)

        vc.play(source, after=after)

        channel = bot.get_channel(state.get("text_channel_id"))
        if channel:
            embed, file = await build_now_playing_embed(meta, spotify_url=None)
            embed.set_footer(text="Autoplaying")
            if file:
                await channel.send(embed=embed, file=file)
            else:
                await channel.send(embed=embed)

    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[autoplay error] {e}")


# ── Playback ──────────────────────────────────────────────────────────────────

async def play_local_file(
    filepath: str,
    meta: dict,
    guild_id: int,
    vc: discord.VoiceClient,
    bot: discord.Client,
    *,
    label: str | None = None,
) -> bool:
    """
    Play an already-downloaded local mp3 file directly in the voice channel.
    Updates voice_states and wires up the after-callback to play_next.
    Returns True if playback started successfully, False otherwise.
    """
    if not filepath or not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
        print(f"[play_local_file] file missing or empty: {filepath}")
        return False

    state = voice_states.get(guild_id)
    if not state:
        return False

    display = label or meta.get("title") or os.path.basename(filepath)

    state["current_file"]  = filepath
    state["current_label"] = display
    state["current_meta"]  = meta
    state["last_title"]    = display

    vol = state.get("volume", 1.0)
    normalize = meta.get("normalize", False)

    if normalize:
        await _apply_loudnorm(filepath)

    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(filepath, before_options="-bufsize 8192k"),
        volume=vol,
    )

    def after(error):
        if error:
            print(f"[play_local_file error] {error}")
        play_next(guild_id, vc, bot)

    vc.play(source, after=after)
    print(f"[play_local_file] playing: {filepath} ({os.path.getsize(filepath) / 1024:.1f}KB)")
    return True


async def _retry_failed_track(
    guild_id: int,
    vc: discord.VoiceClient,
    bot: discord.Client,
    label: str,
    meta: dict,
):
    """
    Called when ffmpeg fails mid-playback. Re-downloads the track and plays it.
    Also sends the mp3 file to the text channel as a fallback if it fits.
    """
    state = voice_states.get(guild_id)
    channel = bot.get_channel(state.get("text_channel_id")) if state else None

    title = meta.get("title") or label
    print(f"[retry] ffmpeg failed for '{title}', re-downloading...")

    if channel:
        await channel.send(f"Playback failed for **{title}**, re-downloading...")

    filepath, new_meta = await search_and_download_audio(f"ytsearch1:{title}")

    if not filepath:
        if channel:
            await channel.send(f"Couldn't re-download **{title}**.")
        play_next(guild_id, vc, bot)
        return

    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    if channel and size_mb <= MAX_FILE_SIZE_MB:
        safe = re.sub(r'[<>:"/\\|?*]', '_', title).strip() or "audio"
        try:
            await channel.send(
                content=f"-# Fallback upload for **{title}**",
                file=discord.File(filepath, filename=f"{safe}.mp3"),
            )
        except Exception as e:
            print(f"[retry] failed to send fallback file: {e}")

    started = await play_local_file(filepath, new_meta or meta, guild_id, vc, bot, label=label)
    if not started:
        if channel:
            await channel.send(f"Still couldn't play **{title}** after re-download.")
        play_next(guild_id, vc, bot)


# ── Queue ─────────────────────────────────────────────────────────────────────

def play_next(guild_id: int, vc: discord.VoiceClient, bot: discord.Client, *, silent: bool = False):
    state = voice_states.get(guild_id)
    if not state:
        return

    if state.get("current_file"):
        try: os.remove(state["current_file"])
        except Exception: pass
        state["current_file"] = None

    state["autoplaying"] = False

    if not state["queue"]:
        task = asyncio.run_coroutine_threadsafe(
            _schedule_autoplay_task(guild_id, vc, bot),
            bot.loop,
        )
        state["autoplay_task"] = task
        return

    _cancel_autoplay(guild_id)

    filepath, label, meta = state["queue"].pop(0)
    print(f"[play_next] {filepath} | exists={os.path.exists(filepath)} | size={os.path.getsize(filepath) if os.path.exists(filepath) else 'MISSING'}")

    if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
        print(f"[play_next] file missing/empty, skipping: {filepath}")
        play_next(guild_id, vc, bot)
        return

    state["current_file"]  = filepath
    state["current_label"] = label
    state["current_meta"]  = meta
    state["last_title"]    = label

    asyncio.run_coroutine_threadsafe(
        _play_next_async(guild_id, vc, bot, filepath, label, meta, silent),
        bot.loop,
    )


async def _play_next_async(
    guild_id: int,
    vc: discord.VoiceClient,
    bot: discord.Client,
    filepath: str,
    label: str,
    meta: dict,
    silent: bool,
):
    state = voice_states.get(guild_id)
    if not state:
        return

    vol = state.get("volume", 1.0)
    normalize = meta.get("normalize", False)

    if normalize:
        await _apply_loudnorm(filepath)

    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(filepath, **FFMPEG_OPTIONS),
        volume=vol,
    )

    def after(error):
        if error:
            print(f"[play_next error] {error}")
            asyncio.run_coroutine_threadsafe(
                _retry_failed_track(guild_id, vc, bot, label, meta),
                bot.loop,
            )
            return
        play_next(guild_id, vc, bot)

    vc.play(source, after=after)

    if state["queue"]:
        next_filepath, _, next_meta = state["queue"][0]
        if next_meta.get("normalize"):
            asyncio.ensure_future(_apply_loudnorm(next_filepath))

    if not silent:
        channel = bot.get_channel(state.get("text_channel_id"))
        if channel:
            embed, file = await build_now_playing_embed(
                meta,
                queued_count=len(state["queue"]),
                spotify_url=meta.get("spotify_url"),
            )
            if file:
                await channel.send(embed=embed, file=file)
            else:
                await channel.send(embed=embed)