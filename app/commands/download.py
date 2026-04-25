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
from config import MAX_FILE_SIZE_MB



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


async def attempt_download(url: str, height: int) -> str | None:
    """Try to download at a given height. Returns filepath or None if too big."""
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
        size_mb = os.path.getsize(filepath) / (1024 * 1024)

        if size_mb > MAX_FILE_SIZE_MB:
            return None

        dest = os.path.join(tempfile.gettempdir(), os.path.basename(filepath))
        shutil.copy2(filepath, dest)
        return dest


async def download_spotify_track(interaction: discord.Interaction, url: str):
    status = await interaction.followup.send("Detected Spotify link, fetching track info...", wait=True)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.song.link/v1-alpha.1/links?url={url}&userCountry=US"
            ) as resp:
                data = await resp.json()

            entities = list(data.get("entitiesByUniqueId", {}).values())

            if not entities:
                async with session.get(
                    f"https://api.song.link/v1-alpha.1/links?url={url}"
                ) as resp:
                    data = await resp.json()
                entities = list(data.get("entitiesByUniqueId", {}).values())

        if not entities:
            await status.edit(content="song.link failed, trying Spotify page...")
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    html = await resp.text()
            og_title = re.search(r'<meta property="og:title" content="([^"]+)"', html)
            og_desc = re.search(r'<meta name="description" content="([^"]+)"', html)
            if og_title:
                raw = og_title.group(1)
                raw = re.sub(r"(?i)listen to (.+) on spotify", r"\1", raw).strip()
                track_name = raw.split(" - ")[0].strip()
                artist = og_desc.group(1).split(" · ")[0].strip() if og_desc else ""
            else:
                await status.edit(content="Couldn't fetch track info from any source.")
                return
            yt_url = None
        else:
            entity = entities[0]
            track_name = entity.get("title")
            artist = entity.get("artistName", "")
            links_by_platform = data.get("linksByPlatform", {})
            yt_music = links_by_platform.get("youtubeMusic", {})
            youtube = links_by_platform.get("youtube", {})
            yt_url = yt_music.get("url") or youtube.get("url") or None

        if not track_name:
            await status.edit(content="Couldn't extract track name.")
            return

    except Exception as e:
        await status.edit(content=f"Couldn't fetch track info: `{e}`")
        return

    clean_artist = artist.split(",")[0].split("&")[0].strip()
    clean_title = re.sub(r"[\(\[].*?[\)\]]", "", track_name).strip()

    search_attempts = []
    if yt_url:
        search_attempts.append((yt_url, f"**{artist} - {track_name}** (exact match)"))
    search_attempts += [
        (f"ytsearch1:{artist} {track_name}", f"**{artist} - {track_name}**"),
        (f"ytsearch1:{clean_artist} {clean_title}", f"**{clean_artist} - {clean_title}** (simplified)"),
        (f"ytsearch1:{clean_title}", f"**{clean_title}** (title only)"),
    ]

    def _run_ydl_with_url(ydl_opts, query):
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=True)
            if info and "entries" in info:
                info = info["entries"][0]
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
                    size_mb = os.path.getsize(filepath) / (1024 * 1024)

                    if size_mb <= MAX_FILE_SIZE_MB:
                        dest = os.path.join(tempfile.gettempdir(), os.path.basename(filepath))
                        shutil.copy2(filepath, dest)
                        source = resolved_url or search_query
                        await status.edit(content=f"Found: **{clean_artist} - {clean_title}**")
                        await interaction.followup.send(file=discord.File(dest, os.path.basename(f"{clean_artist} - {clean_title}.mp3")), content=f"-# Source: <{source}>")
                        asyncio.create_task(delayed_delete(status, delay=5))
                        try:
                            os.remove(dest)
                        except Exception:
                            pass
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
        quality="Video quality (default: auto picks 720p or 480p based on file size)",
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
                size_mb = os.path.getsize(filepath) / (1024 * 1024)
                if size_mb > MAX_FILE_SIZE_MB:
                    await interaction.followup.send(f"Audio came out {size_mb:.1f}MB, too big to upload")
                    return

                await interaction.followup.send(file=discord.File(filepath, os.path.basename(filepath)))
                return

        try:
            filepath = None

            if quality == "auto":
                resolutions = [1080, 720, 480, 360]
                for res in resolutions:
                    res_msg = await interaction.followup.send(f"trying {res}p...", wait=True)
                    filepath = await attempt_download(url, res)
                    if filepath:
                        await res_msg.edit(content=f"Success at {res}p!")
                        asyncio.create_task(delayed_delete(res_msg, delay=1))
                        break

                else:
                    await interaction.followup.send("track is too large to upload even at lowest quality.")
                    return
            else:
                filepath = await attempt_download(url, int(quality))
                if not filepath:
                    await interaction.followup.send(
                        f"{quality}p is over Discord's {MAX_FILE_SIZE_MB}MB limit. try a lower quality or use auto"
                    )
                    return

            await interaction.followup.send(file=discord.File(filepath, os.path.basename(filepath)))

            try:
                os.remove(filepath)
            except Exception:
                pass

        except Exception as e:
            await interaction.followup.send(f"Couldn't download video: `{e}`")
