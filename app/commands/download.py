import os
import re
import glob
import shutil
import asyncio
import secrets
import tempfile
import aiohttp
import yt_dlp
import discord
from discord import app_commands
from config import MAX_FILE_SIZE_MB, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, FILE_SERVER_PATH, FILE_SERVER_BASE_URL, FILE_EXPIRY_SECONDS



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


def get_video_opts(outtmpl: str, height: int) -> dict:
    return {
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreerrors": False,
        "format": (
            f"bestvideo[ext=mp4][height<={height}]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={height}]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={height}]+bestaudio"
            f"/best[height<={height}]"
            f"/best"
        ),
        "merge_output_format": "mp4",
        "postprocessors": [{
            "key": "FFmpegVideoRemuxer",
            "preferedformat": "mp4",
        }],
    }


def _first_entry(info: dict | None) -> dict:
    if not info:
        return {}
    if "entries" in info:
        entries = info["entries"]
        if not entries:
            return {}
        return entries[0] or {}
    return info


# ── Download command helpers ───────────────────────────────────────────────────

def _probe(filepath: str) -> tuple[float, int]:
    import subprocess, json
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", filepath,
        ], timeout=30)
        info     = json.loads(out)
        duration = float(info["format"].get("duration", 0))
        height   = 0
        for stream in info.get("streams", []):
            if stream.get("codec_type") == "video":
                height = int(stream.get("height", 0))
                break
        return duration, height
    except Exception:
        return 0.0, 0


def _needs_remux(filepath: str) -> tuple[bool, float]:
    import subprocess, json
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", filepath,
        ], timeout=30)
        streams = json.loads(out).get("streams", [])

        for s in streams:
            if s.get("codec_type") == "audio" and s.get("codec_name") == "aac":
                profile = s.get("profile", "").lower()
                if "he" not in profile:
                    continue
                try:
                    duration_ts = float(s["duration_ts"])
                    nb_frames   = float(s["nb_frames"])
                    samples_per_frame = duration_ts / nb_frames
                    if samples_per_frame > 3000:
                        return True, 0.5
                except (KeyError, ValueError, ZeroDivisionError):
                    pass

        return False, 1.0
    except Exception:
        return False, 1.0


def _remux_fix(src: str, dest: str, pts_mul: float = 1.0) -> bool:
    import subprocess, os
    wav = src + "_audio_raw.wav"
    try:
        r1 = subprocess.run([
            "ffmpeg", "-y",
            "-i", src,
            "-vn", "-c:a", "pcm_s16le",
            "-ar", "22050",
            "-ac", "2",
            wav,
        ], capture_output=True, timeout=120)
        if r1.returncode != 0 or not os.path.exists(wav):
            return False

        r2 = subprocess.run([
            "ffmpeg", "-y",
            "-i", src,
            "-i", wav,
            "-map", "0:v", "-map", "1:a",
            "-vf", f"setpts={pts_mul:.10g}*PTS",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-video_track_timescale", "90000",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            "-movflags", "+faststart",
            dest,
        ], capture_output=True, timeout=300)
        return r2.returncode == 0 and os.path.exists(dest)
    except Exception:
        return False
    finally:
        try:
            os.remove(wav)
        except Exception:
            pass


def _compress_to_target(src: str, dest: str, target_mb: float, duration_s: float, src_height: int) -> bool:
    import subprocess

    headroom    = 0.90
    target_bits = target_mb * 1024 * 1024 * 8 * headroom
    audio_bps   = 96_000
    video_bps   = max(100_000, int(target_bits / duration_s) - audio_bps)

    if src_height > 480 and video_bps < 500_000:
        scale = "scale=-2:480"
    elif src_height > 720 and video_bps < 1_000_000:
        scale = "scale=-2:720"
    else:
        scale = "scale=trunc(iw/2)*2:trunc(ih/2)*2"

    cmd = [
        "ffmpeg", "-y", "-i", src,
        "-vf", scale,
        "-c:v", "libx264", "-preset", "ultrafast", "-b:v", str(video_bps),
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
        "-f", "mp4", dest,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        return result.returncode == 0 and os.path.exists(dest)
    except Exception:
        return False


def copy_to_file_server(src: str, preferred_name: str = "") -> str | None:
    """
    Copy `src` to the file server's watched folder.
    If `preferred_name` is provided, uses it as the filename (sanitized);
    otherwise falls back to a random 256-bit hex token.
    Remuxes with faststart so browsers can stream it.
    Returns a direct download URL, or None on failure.
    Schedules auto-deletion after FILE_EXPIRY_SECONDS.
    """
    import subprocess
    try:
        os.makedirs(FILE_SERVER_PATH, exist_ok=True)
        ext = os.path.splitext(src)[1]  # preserve .mp4 etc.

        if preferred_name:
            # Sanitize: strip path separators and other dangerous chars
            safe = re.sub(r'[\\/:*?"<>|]', "_", preferred_name).strip(" .")
            hashed_name = f"{safe}{ext}" if safe else secrets.token_hex(32) + ext
        else:
            hashed_name = secrets.token_hex(32) + ext  # 64-char random hex (256-bit)

        dest = os.path.join(FILE_SERVER_PATH, hashed_name)
        # Remux with faststart so browsers can stream without downloading the whole file
        result = subprocess.run([
            "ffmpeg", "-y", "-i", src,
            "-c", "copy",
            "-movflags", "+faststart",
            dest,
        ], capture_output=True, timeout=120)
        if result.returncode != 0 or not os.path.exists(dest):
            # Fall back to plain copy if remux fails
            shutil.copy2(src, dest)
        # Schedule deletion after expiry
        asyncio.create_task(_delete_after(dest, FILE_EXPIRY_SECONDS))
        return f"{FILE_SERVER_BASE_URL}/downloads/{hashed_name}"
    except Exception:
        return None


async def _delete_after(filepath: str, delay: float):
    """Delete `filepath` after `delay` seconds."""
    await asyncio.sleep(delay)
    try:
        os.remove(filepath)
    except Exception:
        pass


async def attempt_download(
    url: str,
    height: int,
    status_msg=None,
    clean_filename: str = "",
) -> tuple[str | None, str | None]:
    """
    Download the best available stream up to `height`p.

    Pipeline:
      - Under 25MB  → (filepath, None)       — attach directly to Discord
      - Over 25MB   → (compressed_path, file_server_url)
                      compressed goes to Discord, original goes to file server
      - Both None   → download failed

    `status_msg` is an optional discord.Message to edit with progress updates.
    `clean_filename` is an optional name to use on the file server instead of a hash.
    """
    async def _status(text: str):
        if status_msg:
            try:
                await status_msg.edit(content=text)
            except Exception:
                pass

    with tempfile.TemporaryDirectory() as tmpdir:
        outtmpl  = os.path.join(tmpdir, "%(title).50s.%(ext)s")
        ydl_opts = get_video_opts(outtmpl, height)

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, lambda: _run_ydl(ydl_opts, url))
        except Exception:
            return None, None

        files = [f for f in glob.glob(os.path.join(tmpdir, "*")) if os.path.isfile(f)]
        if not files:
            return None, None

        src       = max(files, key=os.path.getsize)
        base_name = os.path.splitext(os.path.basename(src))[0] + ".mp4"

        # ── HE-AAC / bad timebase fix ─────────────────────────────────────────
        needs_fix, pts_mul = await loop.run_in_executor(None, lambda: _needs_remux(src))
        if needs_fix:
            await _status(f"Detected HE-AAC or bad timebase — remuxing (pts×{pts_mul:.4g})…")
            fixed = os.path.join(tmpdir, "fixed_" + base_name)
            ok = await loop.run_in_executor(None, lambda: _remux_fix(src, fixed, pts_mul))
            if ok:
                src = fixed

        size_mb = os.path.getsize(src) / (1024 * 1024)

        # ── Under limit — send directly ───────────────────────────────────────
        if size_mb <= MAX_FILE_SIZE_MB:
            dest = os.path.join(tempfile.gettempdir(), base_name)
            shutil.copy2(src, dest)
            return dest, None

        # ── Over limit — compress for Discord, send original to file server ───
        await _status(f"File is {size_mb:.1f} MB — compressing video for Discord")
        duration, src_height = await loop.run_in_executor(None, lambda: _probe(src))

        # Copy original to file server first (while still in tmpdir)
        file_server_url: str | None = None
        raw_dest = os.path.join(tempfile.gettempdir(), base_name)
        shutil.copy2(src, raw_dest)

    # tmpdir gone — work with raw_dest from here
    # Copy to file server first so we have the original safe
    file_server_url = copy_to_file_server(raw_dest, preferred_name=clean_filename)

    # Now compress from raw_dest for Discord
    if duration > 0:
        compressed = os.path.join(tempfile.gettempdir(), "compressed_" + base_name)
        ok = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _compress_to_target(raw_dest, compressed, MAX_FILE_SIZE_MB, duration, src_height)
        )

        try:
            os.remove(raw_dest)
        except Exception:
            pass

        if ok:
            comp_size = os.path.getsize(compressed) / (1024 * 1024)
            if comp_size <= MAX_FILE_SIZE_MB:
                # Compressed for Discord + original on file server
                return compressed, file_server_url

            try:
                os.remove(compressed)
            except Exception:
                pass
    else:
        try:
            os.remove(raw_dest)
        except Exception:
            pass

    # Compression failed/impossible — file server only, nothing for Discord
    return None, file_server_url


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


# ── Command registration ───────────────────────────────────────────────────────

def setup(tree: app_commands.CommandTree):
    @tree.command(name="download", description="Download media from a URL")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        url="The link to download from",
        quality="Video quality (default: auto picks best quality under file size limit)",
        audio_only="Extract audio only (mp3)",
        filename="Custom filename (without extension)"
    )
    @app_commands.choices(quality=[
        app_commands.Choice(name="1080p", value="1080"),
        app_commands.Choice(name="720p",  value="720"),
        app_commands.Choice(name="480p",  value="480"),
        app_commands.Choice(name="360p",  value="360"),
        app_commands.Choice(name="auto",  value="auto"),
    ])
    async def download_media(
        interaction: discord.Interaction,
        url: str,
        quality: str = "auto",
        audio_only: bool = False,
        filename: str = "",
    ):
        await interaction.response.defer(thinking=True)

        if "spotify.com" in url or "open.spotify.com" in url:
            await download_spotify_track(interaction, url)
            return

        clean_filename = re.sub(r'[\\/:*?"<>|]', "_", filename).strip(" .") if filename else ""

        # ── Audio-only path ────────────────────────────────────────────────────
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
                    await interaction.followup.send(f"Audio came out {size_mb:.1f} MB, too big to upload.")
                    return

                ext          = os.path.splitext(filepath)[1]
                display_name = f"{clean_filename}{ext}" if clean_filename else os.path.basename(filepath)
                await interaction.followup.send(file=discord.File(filepath, display_name))
                return

        # ── Video path ─────────────────────────────────────────────────────────
        filepath   = None
        fs_url     = None
        status_msg = await interaction.followup.send("Starting download…", wait=True)
        try:
            target_height = 1080 if quality == "auto" else int(quality)
            filepath, fs_url = await attempt_download(url, target_height, status_msg, clean_filename)

            if not filepath and not fs_url:
                await status_msg.edit(
                    content="Download failed — check the URL."
                    if quality == "auto"
                    else f"Couldn't download at {quality}p — check the URL or try **auto**."
                )
                return

            if filepath:
                ext          = os.path.splitext(filepath)[1]
                display_name = f"{clean_filename}{ext}" if clean_filename else os.path.basename(filepath)
                await status_msg.edit(content="Uploading…")

                if fs_url:
                    # Over 25MB: send compressed to Discord + original link
                    await interaction.followup.send(
                        file=discord.File(filepath, display_name),
                        content=f"-# Compressed for Discord. Original quality: <{fs_url}>"
                    )
                else:
                    # Under 25MB: send normally
                    await interaction.followup.send(file=discord.File(filepath, display_name))

                asyncio.create_task(delayed_delete(status_msg, delay=1))

            elif fs_url:
                # Compression failed entirely — file server only
                await status_msg.edit(
                    content=f"Couldn't compress to fit Discord.\nOriginal quality: <{fs_url}>"
                )

        except Exception as e:
            await interaction.followup.send(f"Couldn't download video: `{e}`")

        finally:
            if filepath:
                try:
                    os.remove(filepath)
                except Exception:
                    pass