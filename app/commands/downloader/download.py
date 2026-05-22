import os
import re
import sys
import glob
import shutil
import asyncio
import secrets
import tempfile
import subprocess
import aiohttp
import yt_dlp
import discord
from discord import app_commands
from config import (
    MAX_FILE_SIZE_MB,
    # SPOTIFY_CLIENT_ID,    # unused with SpotipyFree default; uncomment if switching to official API
    # SPOTIFY_CLIENT_SECRET,
    FILE_SERVER_PATH,
    FILE_SERVER_BASE_URL,
    FILE_EXPIRY_SECONDS,
    NORMALIZE_AUDIO,
    SPOTIFY_PLAYLIST_MAX_SONGS,
)
from .video_fix import _probe, _needs_remux, _remux_fix, _fix_video_pts, _compress_to_target, _normalize_audio


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


def _pack_zip(files: list[str], zip_path: str) -> None:
    """Pack a list of files into a zip archive."""
    import zipfile as _zipfile
    with _zipfile.ZipFile(zip_path, "w", _zipfile.ZIP_DEFLATED) as zf:
        for fp in files:
            zf.write(fp, os.path.basename(fp))


def _is_spotify_playlist_url(url: str) -> bool:
    """Returns True if the URL points to a playlist, album, or artist (multi-track)."""
    return bool(re.search(r"spotify\.com/(playlist|album|artist)/", url))


# ── Download command helpers ───────────────────────────────────────────────────

def copy_to_file_server(src: str, preferred_name: str = "") -> str | None:
    """
    Copy `src` to the file server's watched folder.
    If `preferred_name` is provided, uses it as the filename (sanitized);
    otherwise falls back to a random 256-bit hex token.
    Remuxes with faststart so browsers can stream it.
    Returns a direct download URL, or None on failure.
    Schedules auto-deletion after FILE_EXPIRY_SECONDS.
    """
    try:
        os.makedirs(FILE_SERVER_PATH, exist_ok=True)
        ext = os.path.splitext(src)[1]

        if preferred_name:
            safe = re.sub(r'[\\/:*?"<>|]', "_", preferred_name).strip(" .")
            hashed_name = f"{safe}{ext}" if safe else secrets.token_hex(32) + ext
        else:
            hashed_name = secrets.token_hex(32) + ext

        dest = os.path.join(FILE_SERVER_PATH, hashed_name)
        result = subprocess.run([
            "ffmpeg", "-y", "-i", src,
            "-c", "copy",
            "-movflags", "+faststart",
            dest,
        ], capture_output=True, timeout=120)
        if result.returncode != 0 or not os.path.exists(dest):
            shutil.copy2(src, dest)
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


# ── spotdl helpers ────────────────────────────────────────────────────────────

def _strip_spotify_url(url: str) -> str:
    """Strip ?si= tracking params that confuse spotdl's arg parser."""  # CHANGED
    return url.split("?")[0] if "spotify.com" in url else url          # CHANGED


def _run_spotdl(url: str, outdir: str) -> tuple[list[str], str]:
    clean_url = _strip_spotify_url(url)

    import json as _json
    _spotdl_cfg = os.path.join(os.path.expanduser("~"), ".spotdl", "config.json")
    try:
        with open(_spotdl_cfg) as _f:
            _cfg = _json.load(_f)
        if _cfg.get("load_config", False):
            _cfg["load_config"] = False
            with open(_spotdl_cfg, "w") as _f:
                _json.dump(_cfg, _f, indent=4)
    except Exception:
        pass

    cmd = [
        sys.executable, "-m", "spotdl",
        "--format", "mp3",
        "--bitrate", "320k",
        "--simple-tui",
        "download",
        clean_url,
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=outdir,
    )

    files = sorted(glob.glob(os.path.join(outdir, "**", "*.mp3"), recursive=True))

    # Combine stdout + stderr so the caller sees everything
    combined_output = "\n".join(filter(None, [result.stdout.strip(), result.stderr.strip()]))
    combined_output += f"\n[exit code: {result.returncode}]"

    return files, combined_output

async def download_spotify_track(
    interaction: discord.Interaction,
    url: str,
    normalize: bool = False,
):
    status = await interaction.followup.send("Fetching track via spotdl…", wait=True)

    loop = asyncio.get_event_loop()
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            files, stderr = await loop.run_in_executor(None, lambda: _run_spotdl(url, tmpdir))
        except Exception as e:
            await status.edit(content=f"spotdl failed: `{e}`")
            return

        if not files:
            err_snippet = stderr.strip().splitlines()[-3:] if stderr.strip() else []
            err_text = "\n".join(err_snippet) if err_snippet else "no output"
            await status.edit(content=f"spotdl returned no files.\n```\n{err_text}\n```")
            return

        filepath = files[0]
        display_name = os.path.basename(filepath)

        # ── Audio normalization ───────────────────────────────────────────────
        if normalize:
            await status.edit(content="Normalizing audio…")
            normed = os.path.join(tmpdir, "normed_" + display_name)
            ok = await loop.run_in_executor(None, lambda: _normalize_audio(filepath, normed))
            if ok:
                filepath = normed

        size_mb = os.path.getsize(filepath) / (1024 * 1024)
        dest = os.path.join(tempfile.gettempdir(), display_name)
        shutil.copy2(filepath, dest)

    # ── Over limit → file server  # CHANGED
    if size_mb > MAX_FILE_SIZE_MB:
        await status.edit(content=f"Track is {size_mb:.1f} MB — sending to file server…")
        fs_url = copy_to_file_server(dest, preferred_name=os.path.splitext(display_name)[0])
        try:
            os.remove(dest)
        except Exception:
            pass
        if fs_url:
            await status.edit(content=f"**{display_name}**\n-# Too large for Discord. Download here: <{fs_url}>")
        else:
            await status.edit(content="Track too large for Discord and file server copy failed.")
        return

    await status.edit(content=f"Downloaded: **{display_name}**")
    await interaction.followup.send(file=discord.File(dest, display_name))
    asyncio.create_task(delayed_delete(status, delay=5))
    try:
        os.remove(dest)
    except Exception:
        pass


async def download_spotify_playlist(
    interaction: discord.Interaction,
    url: str,
    normalize: bool = False,
):
    import zipfile as _zipfile

    status = await interaction.followup.send(
        f"Fetching playlist via spotdl (max {SPOTIFY_PLAYLIST_MAX_SONGS} songs)…",
        wait=True,
    )

    loop = asyncio.get_event_loop()
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            all_files, stderr = await loop.run_in_executor(None, lambda: _run_spotdl(url, tmpdir))
        except Exception as e:
            await status.edit(content=f"spotdl failed: `{e}`")
            return

        if not all_files:
            err_snippet = stderr.strip().splitlines()[-3:] if stderr.strip() else []
            err_text = "\n".join(err_snippet) if err_snippet else "no output"
            await status.edit(content=f"spotdl returned no files.\n```\n{err_text}\n```")
            return

        files = all_files[:SPOTIFY_PLAYLIST_MAX_SONGS]
        capped = len(all_files) > SPOTIFY_PLAYLIST_MAX_SONGS

        # ── Normalization ─────────────────────────────────────────────────────
        if normalize:
            await status.edit(content=f"Normalizing {len(files)} track(s)…")
            normed = []
            for fp in files:
                n = os.path.join(tmpdir, "normed_" + os.path.basename(fp))
                ok = await loop.run_in_executor(None, lambda p=fp, nv=n: _normalize_audio(p, nv))
                normed.append(n if ok else fp)
            files = normed

        # ── Pack zip while files are still in tmpdir ──────────────────────────
        await status.edit(content=f"Packing {len(files)} track(s) into zip…")
        zip_name = f"playlist_{secrets.token_hex(6)}.zip"
        zip_path = os.path.join(tmpdir, zip_name)
        await loop.run_in_executor(None, lambda: _pack_zip(files, zip_path))

        zip_mb = os.path.getsize(zip_path) / (1024 * 1024)
        zip_dest = os.path.join(tempfile.gettempdir(), zip_name)
        shutil.copy2(zip_path, zip_dest)

    # tmpdir + all mp3s deleted here; zip_dest still alive
    summary = f"**{len(files)} track(s)** packed."
    if capped:
        summary += f" (capped at {SPOTIFY_PLAYLIST_MAX_SONGS}/{len(all_files)} — adjust `SPOTIFY_PLAYLIST_MAX_SONGS` in config)"

    try:
        if zip_mb <= MAX_FILE_SIZE_MB:
            await status.edit(content=summary)
            await interaction.followup.send(file=discord.File(zip_dest, zip_name))
        else:
            await status.edit(content=f"{summary} Zip is {zip_mb:.1f} MB — sending to file server…")
            fs_url = copy_to_file_server(zip_dest, preferred_name=os.path.splitext(zip_name)[0])
            if fs_url:
                await status.edit(content=f"{summary}\n-# Too large for Discord. Download here: <{fs_url}>")
            else:
                await status.edit(content="Zip too large for Discord and file server copy failed.")
    finally:
        try:
            os.remove(zip_dest)
        except Exception:
            pass


# ── attempt_download (video) ──────────────────────────────────────────────────

async def attempt_download(
    url: str,
    height: int,
    status_msg=None,
    clean_filename: str = "",
    normalize: bool = False,
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

        # ── HE-AAC / bad timebase / wrong video PTS fix ───────────────────────
        needs_fix, pts_mul, fix_type = await loop.run_in_executor(None, lambda: _needs_remux(src))
        if needs_fix:
            if fix_type == "video":
                await _status(f"Detected video PTS mismatch — fixing video speed (pts×{pts_mul:.4g})…")
                fixed = os.path.join(tmpdir, "fixed_" + base_name)
                ok = await loop.run_in_executor(None, lambda: _fix_video_pts(src, fixed, pts_mul))
            else:
                await _status(f"Detected audio timing issue — remuxing (pts×{pts_mul:.4g})…")
                fixed = os.path.join(tmpdir, "fixed_" + base_name)
                ok = await loop.run_in_executor(None, lambda: _remux_fix(src, fixed, pts_mul))
            if ok:
                src = fixed

        # ── Audio normalization ───────────────────────────────────────────────
        if normalize:
            await _status("Normalizing audio…")
            normed = os.path.join(tmpdir, "normed_" + base_name)
            ok = await loop.run_in_executor(None, lambda: _normalize_audio(src, normed))
            if ok:
                src = normed

        size_mb = os.path.getsize(src) / (1024 * 1024)

        # ── Under limit — send directly ───────────────────────────────────────
        if size_mb <= MAX_FILE_SIZE_MB:
            dest = os.path.join(tempfile.gettempdir(), base_name)
            shutil.copy2(src, dest)
            return dest, None

        # ── Over limit — compress for Discord, send original to file server ───
        await _status(f"File is {size_mb:.1f} MB — compressing video for Discord")
        duration, src_height = await loop.run_in_executor(None, lambda: _probe(src))

        raw_dest = os.path.join(tempfile.gettempdir(), base_name)
        shutil.copy2(src, raw_dest)

    # tmpdir gone — work with raw_dest from here
    file_server_url = copy_to_file_server(raw_dest, preferred_name=clean_filename)

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

    return None, file_server_url


# ── Command registration ───────────────────────────────────────────────────────

def setup(tree: app_commands.CommandTree):
    @tree.command(name="download", description="Download media from a URL")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        url="The link to download from",
        quality="Video quality (default: auto picks best quality under file size limit)",
        audio_only="Extract audio only (mp3)",
        filename="Custom filename (without extension)",
        normalize="Normalize audio loudness to -14 LUFS (default: per config)",
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
        normalize: bool = NORMALIZE_AUDIO,
    ):
        await interaction.response.defer(thinking=True)

        # ── Spotify routing ────────────────────────────────────────────────────
        if "spotify.com" in url or "open.spotify.com" in url:  # CHANGED
            if _is_spotify_playlist_url(url):                   # CHANGED
                await download_spotify_playlist(interaction, url, normalize)  # CHANGED
            else:                                               # CHANGED
                await download_spotify_track(interaction, url, normalize)     # CHANGED
            return                                              # CHANGED

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

                if normalize:
                    normed = os.path.join(tmpdir, "normed_" + os.path.basename(filepath))
                    ok = await loop.run_in_executor(None, lambda: _normalize_audio(filepath, normed))
                    if ok:
                        filepath = normed

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
            filepath, fs_url = await attempt_download(url, target_height, status_msg, clean_filename, normalize)

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
                    await interaction.followup.send(
                        file=discord.File(filepath, display_name),
                        content=f"-# Compressed for Discord. Original quality: <{fs_url}>"
                    )
                else:
                    await interaction.followup.send(file=discord.File(filepath, display_name))

                asyncio.create_task(delayed_delete(status_msg, delay=1))

            elif fs_url:
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