import os
import re
import glob
import shutil
import asyncio
import tempfile
import aiohttp
import yt_dlp
import discord
from discord import app_commands
from config import MAX_FILE_SIZE_MB, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET


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
        # Prefer mp4/m4a streams; fall back to anything at or below target height.
        # The final /best fallback has no height cap — attempt_download checks size.
        "format": (
            f"bestvideo[ext=mp4][height<={height}]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={height}]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={height}]+bestaudio"
            f"/best[height<={height}]"
            f"/best"
        ),
        # Always remux to mp4 — this kills the mp42/mov container fallback issue.
        "merge_output_format": "mp4",
        "postprocessors": [{
            "key": "FFmpegVideoRemuxer",
            "preferedformat": "mp4",
        }],
    }


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


# ── Download command helpers ───────────────────────────────────────────────────

def _probe(filepath: str) -> tuple[float, int]:
    """
    Return (duration_seconds, height_px) via ffprobe.
    Falls back to (0.0, 0) on any error.
    """
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
    """
    Detect broken HE-AACv2 muxing where audio frame duration is 2x inflated.

    HE-AAC uses 2048 samples/frame. A broken mux writes 4096 samples/frame
    (duration_ts/nb_frames ≈ 4096 at the stream sample rate), causing the
    video to play at half speed relative to audio.

    Fine HE-AACv2 files have correct ~2048 samples/frame and must not be touched.
    """
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
                    # HE-AAC correct = ~2048; broken mux = ~4096 (2x inflated)
                    if samples_per_frame > 3000:   # safely between 2048 and 4096
                        return True, 0.5
                except (KeyError, ValueError, ZeroDivisionError):
                    pass

        return False, 1.0
    except Exception:
        return False, 1.0


def _remux_fix(src: str, dest: str, pts_mul: float = 1.0) -> bool:
    """
    Fix HE-AACv2 A/V desync:
      1. Decode HE-AACv2 → WAV at native 22050 Hz core rate (correct sample count)
      2. Re-encode video with setpts=0.5 + encode WAV→AAC-LC at 44100 Hz
         The WAV has the right number of samples at 22050 Hz; encoding to 44100 Hz
         resamples up 2x producing exactly the correct duration in the output.
    """
    import subprocess, os
    wav = src + "_audio_raw.wav"
    try:
        # Step 1: decode HE-AACv2 to raw PCM at its true core rate
        # Do NOT let ffmpeg auto-handle SBR — force 22050 Hz output
        r1 = subprocess.run([
            "ffmpeg", "-y",
            "-i", src,
            "-vn", "-c:a", "pcm_s16le",
            "-ar", "22050",   # force core rate, not SBR-doubled rate
            "-ac", "2",
            wav,
        ], capture_output=True, timeout=120)
        if r1.returncode != 0 or not os.path.exists(wav):
            return False

        # Step 2: setpts on video + encode WAV (22050 Hz) → AAC-LC (44100 Hz)
        # ffmpeg resamples 22050→44100 which doubles sample count = correct duration
        r2 = subprocess.run([
            "ffmpeg", "-y",
            "-i", src,          # video source
            "-i", wav,          # corrected audio source (22050 Hz PCM)
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
    """
    Re-encode `src` → `dest` (mp4) targeting `target_mb`.

    Speed tricks:
    - `ultrafast` preset  — ~5-10x faster than default, negligible size difference
      at low bitrates where we're already crunching hard.
    - Auto scale-down     — if the required bitrate is very low, drop to a smaller
      resolution so the encoder isn't wasting cycles on pixels it has no bits for.
      Thresholds: <500 kbps → 480p, <1000 kbps → 720p, else keep source height.
    - No -maxrate/-bufsize — those force the encoder to be conservative and slow.
    """
    import subprocess

    headroom    = 0.90
    target_bits = target_mb * 1024 * 1024 * 8 * headroom
    audio_bps   = 96_000   # 96k — inaudible difference at these sizes
    video_bps   = max(100_000, int(target_bits / duration_s) - audio_bps)

    # Pick an output resolution that makes sense for the available bitrate.
    if src_height > 480 and video_bps < 500_000:
        scale = "scale=-2:480"
    elif src_height > 720 and video_bps < 1_000_000:
        scale = "scale=-2:720"
    else:
        scale = "scale=trunc(iw/2)*2:trunc(ih/2)*2"  # ensure even dims, no resize

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


async def upload_to_filehost(filepath: str) -> str | None:
    """
    Upload `filepath` to 0x0.st and return the direct URL, or None on failure.
    Files expire based on size (large files ~a few days, small ones up to a year).
    """
    try:
        async with aiohttp.ClientSession() as session:
            with open(filepath, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("file", f, filename=os.path.basename(filepath))
                async with session.post("https://0x0.st", data=data, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                    if resp.status == 200:
                        return (await resp.text()).strip()
    except Exception:
        pass
    return None


async def attempt_download(
    url: str,
    height: int,
    status_msg=None,
) -> tuple[str | None, str | None]:
    """
    Download the best available stream up to `height`p.

    Returns (filepath, hosted_url) where:
      - filepath is a local .mp4 ready to attach to Discord (caller must delete), or None
      - hosted_url is a 0x0.st link used as a last resort when the file is too big
        to upload directly even after compression, or None

    If both are None the download itself failed.
    `status_msg` is an optional discord.Message to edit with progress updates.
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

        src      = max(files, key=os.path.getsize)
        base_name = os.path.splitext(os.path.basename(src))[0] + ".mp4"

        # ── HE-AAC / bad timebase fix ─────────────────────────────────────────
        needs_fix, pts_mul = await loop.run_in_executor(None, lambda: _needs_remux(src))
        if needs_fix:
            await _status(f"Detected HE-AAC or bad timebase — remuxing (pts×{pts_mul:.4g})…")
            fixed = os.path.join(tmpdir, "fixed_" + base_name)
            ok = await loop.run_in_executor(None, lambda: _remux_fix(src, fixed, pts_mul))
            if ok:
                src = fixed

        size_mb  = os.path.getsize(src) / (1024 * 1024)

        # ── Already fits — just copy out ──────────────────────────────────────
        if size_mb <= MAX_FILE_SIZE_MB:
            dest = os.path.join(tempfile.gettempdir(), base_name)
            shutil.copy2(src, dest)
            return dest, None

        # ── Too big — try FFmpeg compression ─────────────────────────────────
        await _status(f"File is {size_mb:.1f} MB, compressing to fit under {MAX_FILE_SIZE_MB} MB…")
        duration, src_height = await loop.run_in_executor(None, lambda: _probe(src))

        if duration > 0:
            compressed = os.path.join(tempfile.gettempdir(), "compressed_" + base_name)
            ok = await loop.run_in_executor(
                None, lambda: _compress_to_target(src, compressed, MAX_FILE_SIZE_MB, duration, src_height)
            )
            if ok:
                comp_size = os.path.getsize(compressed) / (1024 * 1024)
                if comp_size <= MAX_FILE_SIZE_MB:
                    return compressed, None
                # Compression didn't hit target — clean up and fall through
                try:
                    os.remove(compressed)
                except Exception:
                    pass

        # ── Compression failed / impossible — upload to file host ─────────────
        await _status(f"Compression couldn't fit under {MAX_FILE_SIZE_MB} MB, uploading to file host…")
        # Copy src out of the about-to-be-deleted tmpdir first
        raw_dest = os.path.join(tempfile.gettempdir(), base_name)
        shutil.copy2(src, raw_dest)

    # tmpdir is now gone; raw_dest is safe
    hosted_url = await upload_to_filehost(raw_dest)
    try:
        os.remove(raw_dest)
    except Exception:
        pass

    return None, hosted_url  # signal to caller: send link instead of file


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

        # Sanitize custom filename — strip path separators and leading dots.
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
        filepath    = None
        hosted_url  = None
        status_msg  = await interaction.followup.send("Starting download…", wait=True)
        try:
            if quality == "auto":
                # With compression + file-host fallback we only need one pass at
                # the best available quality — attempt_download handles the rest.
                filepath, hosted_url = await attempt_download(url, 1080, status_msg)
                if not filepath and not hosted_url:
                    await status_msg.edit(content="Download failed — check the URL.")
                    return
            else:
                filepath, hosted_url = await attempt_download(url, int(quality), status_msg)
                if not filepath and not hosted_url:
                    await status_msg.edit(
                        content=f"Couldn't download at {quality}p — check the URL or try **auto**."
                    )
                    return

            if hosted_url:
                # File was too large even after compression; send external link.
                await status_msg.edit(
                    content=f"File too large to attach even after compression.\n🔗 {hosted_url}\n-# Hosted on 0x0.st — link expires after a few days."
                )
                return

            # Normal attach path.
            ext          = os.path.splitext(filepath)[1]
            display_name = f"{clean_filename}{ext}" if clean_filename else os.path.basename(filepath)
            await status_msg.edit(content="Uploading…")
            await interaction.followup.send(file=discord.File(filepath, display_name))
            asyncio.create_task(delayed_delete(status_msg, delay=1))

        except Exception as e:
            await interaction.followup.send(f"Couldn't download video: `{e}`")

        finally:
            # Clean up regardless of success or error.
            if filepath:
                try:
                    os.remove(filepath)
                except Exception:
                    pass