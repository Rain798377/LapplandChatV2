import os
import io
import re
import glob
import shutil
import asyncio
import tempfile
import aiohttp
import yt_dlp
import discord
import random
from discord import app_commands
from config import MAX_FILE_SIZE_MB, AUTOPLAY_DELAY


FFMPEG_OPTIONS = {
    "options": "-vn",
}

AUTOPLAY_QUERIES = [
    "ytsearch1:{title} official audio",
    "ytsearch1:songs like {title}",
    "ytsearch1:{title} similar songs mix",
    "ytsearch1:{title} type beat",
]

voice_states = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

async def delayed_delete(*messages, delay: float = 1):
    await asyncio.sleep(delay)
    for msg in messages:
        try:
            await msg.delete()
        except Exception:
            pass


def _run_ydl(opts: dict, url: str):
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])


def get_audio_opts(outtmpl: str) -> dict:
    return {
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "0",
        }],
    }


def _safe_filename(name: str) -> str:
    """Strip special chars and trailing spaces that break ffmpeg on Windows."""
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = name.strip()
    return name or "audio"


def _first_entry(info: dict | None) -> dict:
    """Safely unwrap a yt-dlp search result, returning {} on empty/missing entries."""
    if not info:
        return {}
    if "entries" in info:
        entries = info["entries"]
        if not entries:
            return {}
        return entries[0] or {}
    return info


def _build_search_attempts(query: str) -> list[str]:
    """
    Given any query, return a list of progressively simplified searches to try.
    - If it's a URL, return it as-is (no fallbacks needed).
    - Otherwise, strip it down step by step until something works.
    """
    if query.startswith("http://") or query.startswith("https://"):
        return [query]

    # Unwrap ytsearch1: prefix so we work with the raw text
    raw = re.sub(r"^ytsearch\d+:", "", query).strip()

    # Check if user explicitly wants a slowed/reverb/etc version
    variant_words = re.compile(
        r"\bslowed\b|\breverb\b|\bultra\s*slowed\b|\bsped\s*up\b|\bnightcore\b",
        re.IGNORECASE,
    )
    user_wants_variant = bool(variant_words.search(raw))

    noise = re.compile(
        r"[\(\[][^\)\]]*[\)\]]"
        r"|\bslowed\b"
        r"|\bultra\s*slowed\b"
        r"|\breverb\b"
        r"|\bsped\s*up\b"
        r"|\bnightcore\b"
        r"|\s*[-–]\s*\w*slowed\w*",
        re.IGNORECASE,
    )

    attempts = []

    if user_wants_variant:
        # User asked for it — try exact first, clean version as fallback
        attempts.append(f"ytsearch1:{raw}")
        cleaned = noise.sub("", raw).strip()
        if cleaned and cleaned != raw:
            attempts.append(f"ytsearch1:{cleaned}")
    else:
        # Strip noise immediately so yt-dlp doesn't serve a slowed version
        cleaned = noise.sub("", raw).strip() or raw
        # Lead with "official audio" so YouTube ranks the original over slowed/reverb versions
        attempts.append(f"ytsearch1:{cleaned} official audio")
        # Fallback without the suffix
        attempts.append(f"ytsearch1:{cleaned}")

    # Strip remaining parentheses/brackets (feat., version tags, etc.)
    base = (cleaned if not user_wants_variant else raw)
    simplified = re.sub(r"[\(\[].*?[\)\]]", "", base).strip()
    if simplified and simplified != base:
        attempts.append(f"ytsearch1:{simplified}")

    # Keep only first segment before a pipe (not dash — dash separates Artist - Title)
    first_segment = re.split(r"\s*\|\s*", simplified or base)[0].strip()
    if first_segment and first_segment != (simplified or base):
        attempts.append(f"ytsearch1:{first_segment}")

    # Deduplicate while preserving order
    seen, unique = set(), []
    for a in attempts:
        if a not in seen:
            seen.add(a)
            unique.append(a)
    return unique


# ── Embed builder ─────────────────────────────────────────────────────────────

async def build_now_playing_embed(meta: dict, queued_count: int = 0) -> tuple[discord.Embed, discord.File | None]:
    title     = meta.get("title")    or "Unknown Title"
    artist    = meta.get("artist")   or "Unknown Artist"
    album     = meta.get("album")
    duration  = meta.get("duration")
    thumbnail = meta.get("thumbnail")

    def fmt_duration(seconds):
        if not seconds:
            return "Unknown"
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"

    embed = discord.Embed(color=0x1DB954)

    # ── Large image at top ────────────────────────────────────────────────────
    file = None
    if isinstance(thumbnail, bytes):
        file = discord.File(io.BytesIO(thumbnail), filename="cover.png")
        embed.set_image(url="attachment://cover.png")
    elif isinstance(thumbnail, str) and thumbnail.startswith("http"):
        embed.set_image(url=thumbnail)

    # ── Title as embed title (renders above fields, below image) ──────────────
    embed.title = title

    # ── Metadata fields below image ───────────────────────────────────────────
    embed.add_field(name="Artist",   value=artist,                 inline=True)
    embed.add_field(name="Duration", value=fmt_duration(duration), inline=True)

    if album:
        embed.add_field(name="Album", value=album, inline=True)

    if queued_count:
        embed.add_field(
            name="Up next",
            value=f"{queued_count} song{'s' if queued_count != 1 else ''}",
            inline=True
        )

    embed.set_footer(text="Now Playing")

    return embed, file


# ── Audio download for voice playback ─────────────────────────────────────────

async def search_and_download_audio(query: str) -> tuple[str, dict] | tuple[None, None]:
    """
    Try to download audio for `query`, falling back to progressively simpler
    searches if the exact query returns no results.
    """

    def _run(ydl_opts, q):
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(q, download=True)
            return _first_entry(info)

    def _make_ydl_opts(outtmpl: str) -> dict:
        return {
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "playlist_items": "1",
            "format": "bestaudio/best",
            "writethumbnail": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "0",
                },
                {"key": "EmbedThumbnail"},
                {"key": "FFmpegMetadata"},
            ],
        }

    loop = asyncio.get_event_loop()
    search_attempts = _build_search_attempts(query)

    for attempt in search_attempts:
        print(f"[search_and_download_audio] trying: {attempt}")
        with tempfile.TemporaryDirectory() as tmpdir:
            ydl_opts = _make_ydl_opts(os.path.join(tmpdir, "%(title).50s.%(ext)s"))

            try:
                info = await loop.run_in_executor(None, lambda: _run(ydl_opts, attempt))
            except Exception as e:
                print(f"[search_and_download_audio] error on '{attempt}': {e}")
                continue

            if not info:
                print(f"[search_and_download_audio] no result for: {attempt}")
                continue

            files = glob.glob(os.path.join(tmpdir, "*.mp3"))
            if not files:
                print(f"[search_and_download_audio] no mp3 produced for: {attempt}")
                continue

            filepath = files[0]
            meta = {"title": None, "artist": None, "album": None, "duration": None, "thumbnail": None}

            # ID3 tags first
            try:
                from mutagen.id3 import ID3
                from mutagen.mp3 import MP3
                tags = ID3(filepath)
                mp3  = MP3(filepath)
                meta["title"]    = str(tags.get("TIT2", "")) or None
                meta["artist"]   = str(tags.get("TPE1", "")) or None
                meta["album"]    = str(tags.get("TALB", "")) or None
                meta["duration"] = int(mp3.info.length)
                apic = tags.get("APIC:") or tags.get("APIC")
                if apic:
                    meta["thumbnail"] = apic.data  # raw bytes
            except Exception:
                pass

            # yt-dlp fallback for anything missing
            if not meta["title"]:    meta["title"]    = info.get("title")
            if not meta["artist"]:   meta["artist"]   = info.get("uploader") or info.get("channel")
            if not meta["duration"]: meta["duration"] = info.get("duration")
            if meta["thumbnail"] is None:
                meta["thumbnail"] = info.get("thumbnail")  # URL string fallback

            safe_name = re.sub(r'[<>:"/\\|?*]', '_', os.path.basename(filepath)).strip()
            safe_name = safe_name or "audio.mp3"
            dest = os.path.join(tempfile.gettempdir(), safe_name)
            shutil.copy2(filepath, dest)

            if not os.path.exists(dest) or os.path.getsize(dest) == 0:
                print(f"[search_and_download_audio] dest missing or empty: {dest}")
                continue

            print(f"[download] saved to: {dest} ({os.path.getsize(dest) / 1024:.1f}KB)")
            return dest, meta

    print(f"[search_and_download_audio] all attempts exhausted for: {query}")
    return None, None


# ── Spotify resolution ────────────────────────────────────────────────────────

async def resolve_spotify_to_query(url: str) -> tuple[str, str] | tuple[None, None]:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.song.link/v1-alpha.1/links?url={url}&userCountry=US"
            ) as resp:
                data = await resp.json()

        entities = list(data.get("entitiesByUniqueId", {}).values())
        links = data.get("linksByPlatform", {})

        if entities:
            entity = entities[0]
            artist = entity.get("artistName", "")
            title  = entity.get("title", "")
            yt_url = links.get("youtubeMusic", {}).get("url") or links.get("youtube", {}).get("url")
            return yt_url or f"ytsearch1:{artist} {title}", f"{artist} - {title}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                html = await resp.text()
        og_title = re.search(r'<meta property="og:title" content="([^"]+)"', html)
        if og_title:
            raw = re.sub(r"(?i)listen to (.+) on spotify", r"\1", og_title.group(1)).strip()
            return f"ytsearch1:{raw}", raw

    except Exception:
        pass

    return None, None


# ── Voice playback ────────────────────────────────────────────────────────────

def _cancel_autoplay(guild_id: int):
    state = voice_states.get(guild_id)
    if state and state.get("autoplay_task"):
        state["autoplay_task"].cancel()
        state["autoplay_task"] = None


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
    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(filepath, **FFMPEG_OPTIONS),
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

    # Send file to chat as a fallback copy if it fits in Discord's limit
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


def play_next(guild_id: int, vc: discord.VoiceClient, bot: discord.Client):
    state = voice_states.get(guild_id)
    if not state:
        return

    if state.get("current_file"):
        try: os.remove(state["current_file"])
        except Exception: pass
        state["current_file"] = None

    if not state["queue"]:
        future = asyncio.run_coroutine_threadsafe(
            _autoplay_after_delay(guild_id, vc, bot),
            bot.loop
        )
        state["autoplay_task"] = future
        return

    _cancel_autoplay(guild_id)

    filepath, label, meta = state["queue"].pop(0)
    print(f"[play_next] {filepath} | exists={os.path.exists(filepath)} | size={os.path.getsize(filepath) if os.path.exists(filepath) else 'MISSING'}")

    # Skip silently if the file vanished before we could play it
    if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
        print(f"[play_next] file missing/empty, skipping: {filepath}")
        play_next(guild_id, vc, bot)
        return

    state["current_file"]  = filepath
    state["current_label"] = label
    state["current_meta"]  = meta
    state["last_title"]    = label

    vol = state.get("volume", 1.0)
    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(filepath, **FFMPEG_OPTIONS),
        volume=vol
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

    # Send Now Playing embed to text channel
    async def _send_now_playing():
        channel = bot.get_channel(state.get("text_channel_id"))
        if not channel:
            return
        embed, file = await build_now_playing_embed(meta, queued_count=len(state["queue"]))
        if file:
            await channel.send(embed=embed, file=file)
        else:
            await channel.send(embed=embed)

    asyncio.run_coroutine_threadsafe(_send_now_playing(), bot.loop)


async def _autoplay_after_delay(guild_id: int, vc: discord.VoiceClient, bot: discord.Client):
    try:
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
            embed, file = await build_now_playing_embed(meta)
            embed.set_footer(text="Autoplaying")
            if file:
                await channel.send(embed=embed, file=file)
            else:
                await channel.send(embed=embed)

    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[autoplay error] {e}")


# ── Download command ───────────────────────────────────────────────────────────

async def attempt_download(url: str, height: int) -> str | None:
    with tempfile.TemporaryDirectory() as tmpdir:
        ydl_opts = {
            "outtmpl": os.path.join(tmpdir, "%(title).50s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": (
                f"bestvideo[ext=mp4][height<={height}]+bestaudio[ext=m4a]"
                f"/bestvideo[height<={height}]+bestaudio"
                f"/best[height<={height}]"
                f"/best"
            ),
            "merge_output_format": "mp4",
        }

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: _run_ydl(ydl_opts, url))

        files = glob.glob(os.path.join(tmpdir, "*"))
        if not files:
            return None

        filepath = files[0]
        if os.path.getsize(filepath) / (1024 * 1024) > MAX_FILE_SIZE_MB:
            return None

        dest = os.path.join(tempfile.gettempdir(), os.path.basename(filepath))
        shutil.copy2(filepath, dest)
        return dest


async def download_spotify_track(interaction: discord.Interaction, url: str):
    status = await interaction.followup.send("Detected Spotify link, fetching track info...", wait=True)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.song.link/v1-alpha.1/links?url={url}&userCountry=US") as resp:
                data = await resp.json()
            entities = list(data.get("entitiesByUniqueId", {}).values())
            if not entities:
                async with session.get(f"https://api.song.link/v1-alpha.1/links?url={url}") as resp:
                    data = await resp.json()
                entities = list(data.get("entitiesByUniqueId", {}).values())

        if not entities:
            await status.edit(content="song.link failed, trying Spotify page...")
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    html = await resp.text()
            og_title = re.search(r'<meta property="og:title" content="([^"]+)"', html)
            og_desc  = re.search(r'<meta name="description" content="([^"]+)"', html)
            if og_title:
                raw = re.sub(r"(?i)listen to (.+) on spotify", r"\1", og_title.group(1)).strip()
                track_name = raw.split(" - ")[0].strip()
                artist = og_desc.group(1).split(" · ")[0].strip() if og_desc else ""
            else:
                await status.edit(content="Couldn't fetch track info from any source.")
                return
            yt_url = None
        else:
            entity     = entities[0]
            track_name = entity.get("title")
            artist     = entity.get("artistName", "")
            links      = data.get("linksByPlatform", {})
            yt_url     = links.get("youtubeMusic", {}).get("url") or links.get("youtube", {}).get("url")

        if not track_name:
            await status.edit(content="Couldn't extract track name.")
            return

    except Exception as e:
        await status.edit(content=f"Couldn't fetch track info: `{e}`")
        return

    clean_artist = artist.split(",")[0].split("&")[0].strip()
    clean_title  = re.sub(r"[\(\[].*?[\)\]]", "", track_name).strip()

    search_attempts = []
    if yt_url:
        search_attempts.append((yt_url, f"**{artist} - {track_name}** (exact match)"))
    search_attempts += [
        (f"ytsearch1:{artist} {track_name}",        f"**{artist} - {track_name}**"),
        (f"ytsearch1:{clean_artist} {clean_title}", f"**{clean_artist} - {clean_title}** (simplified)"),
        (f"ytsearch1:{clean_title}",                f"**{clean_title}** (title only)"),
    ]

    def _run_ydl_with_url(ydl_opts, query):
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=True)
            info = _first_entry(info)
            return info.get("webpage_url") or info.get("url") if info else None

    for search_query, label in search_attempts:
        await status.edit(content=f"Searching YouTube for: {label}...")
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                for quality in ["0", "128"]:
                    ydl_opts = {
                        "outtmpl": os.path.join(tmpdir, "%(title).50s.%(ext)s"),
                        "quiet": True,
                        "no_warnings": True,
                        "noplaylist": True,
                        "playlist_items": "1",
                        "format": "bestaudio/best",
                        "postprocessors": [{
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": quality,
                        }],
                    }
                    loop = asyncio.get_event_loop()
                    resolved_url = await loop.run_in_executor(None, lambda: _run_ydl_with_url(ydl_opts, search_query))

                    files = glob.glob(os.path.join(tmpdir, "*.mp3"))
                    if not files:
                        break

                    filepath = files[0]
                    size_mb  = os.path.getsize(filepath) / (1024 * 1024)

                    if size_mb <= MAX_FILE_SIZE_MB:
                        dest   = os.path.join(tempfile.gettempdir(), os.path.basename(filepath))
                        shutil.copy2(filepath, dest)
                        source = resolved_url or search_query
                        await status.edit(content=f"Found: **{clean_artist} - {clean_title}**")
                        await interaction.followup.send(
                            file=discord.File(dest, os.path.basename(f"{clean_artist} - {clean_title}.mp3")),
                            content=f"-# Source: <{source}>"
                        )
                        asyncio.create_task(delayed_delete(status, delay=5))
                        try: os.remove(dest)
                        except Exception: pass
                        return

                    os.remove(filepath)
                    await status.edit(content=f"Best quality too large ({size_mb:.1f}MB), trying lower quality...")
                else:
                    await status.edit(content="Track is too large to upload even at lower quality.")
                    return
            except Exception:
                continue

    await status.edit(content="Couldn't find the track on YouTube after multiple attempts.")


def setup(tree: app_commands.CommandTree):
    @tree.command(name="download", description="Download media from a URL")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        url="The link to download from",
        quality="Video quality (default: auto picks best quality under file size limit)",
        audio_only="Extract audio only (mp3)"
    )
    @app_commands.choices(quality=[
        app_commands.Choice(name="1080p", value="1080"),
        app_commands.Choice(name="720p",  value="720"),
        app_commands.Choice(name="480p",  value="480"),
        app_commands.Choice(name="360p",  value="360"),
        app_commands.Choice(name="auto",  value="auto"),
    ])
    async def download_media(interaction: discord.Interaction, url: str, quality: str = "auto", audio_only: bool = False):
        await interaction.response.defer(thinking=True)

        if "spotify.com" in url or "open.spotify.com" in url:
            await download_spotify_track(interaction, url)
            return

        if audio_only:
            with tempfile.TemporaryDirectory() as tmpdir:
                ydl_opts = get_audio_opts(os.path.join(tmpdir, "%(title).50s.%(ext)s"))
                ydl_opts["noplaylist"] = True
                try:
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, lambda: _run_ydl(ydl_opts, url))
                except Exception as e:
                    await interaction.followup.send(f"Couldn't download audio: `{e}`")
                    return

                files = glob.glob(os.path.join(tmpdir, "*"))
                if not files:
                    await interaction.followup.send("Empty, check URL.")
                    return

                filepath = files[0]
                size_mb  = os.path.getsize(filepath) / (1024 * 1024)
                if size_mb > MAX_FILE_SIZE_MB:
                    await interaction.followup.send(f"Audio came out {size_mb:.1f}MB, too big to upload")
                    return

                await interaction.followup.send(file=discord.File(filepath, os.path.basename(filepath)))
                return

        try:
            filepath = None
            if quality == "auto":
                for res in [1080, 720, 480, 360]:
                    res_msg  = await interaction.followup.send(f"trying {res}p...", wait=True)
                    filepath = await attempt_download(url, res)
                    if filepath:
                        await res_msg.edit(content=f"Success at {res}p!")
                        asyncio.create_task(delayed_delete(res_msg, delay=1))
                        break
                else:
                    await interaction.followup.send("Track is too large to upload even at lowest quality.")
                    return
            else:
                filepath = await attempt_download(url, int(quality))
                if not filepath:
                    await interaction.followup.send(
                        f"{quality}p is over Discord's {MAX_FILE_SIZE_MB}MB limit. Try a lower quality or use auto."
                    )
                    return

            await interaction.followup.send(file=discord.File(filepath, os.path.basename(filepath)))
            try: os.remove(filepath)
            except Exception: pass

        except Exception as e:
            await interaction.followup.send(f"Couldn't download video: `{e}`")