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
import time
from discord import app_commands
from config import MAX_FILE_SIZE_MB, AUTOPLAY_DELAY, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET


FFMPEG_OPTIONS = {
    "options": "-vn",
}

# Normalization filter for real-time streaming.
# loudnorm (EBU R128) requires a two-pass analysis of the full file — it doesn't work
# correctly when ffmpeg is streaming PCM to a pipe, producing little or no audible effect.
# dynaudnorm works frame-by-frame in a single pass, which is correct for piped streaming.
FFMPEG_OPTIONS_NORMALIZED = {
    "options": "-af dynaudnorm=p=0.9:m=100:s=5",
}

# ── Spotify API ───────────────────────────────────────────────────────────────

_spotify_token: str | None = None
_spotify_token_expiry: float = 0


async def _get_spotify_token(client_id: str, client_secret: str) -> str | None:
    """Fetch a client-credentials token, reusing it until it expires."""
    global _spotify_token, _spotify_token_expiry
    if _spotify_token and time.monotonic() < _spotify_token_expiry:
        return _spotify_token
    try:
        import base64
        credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://accounts.spotify.com/api/token",
                headers={"Authorization": f"Basic {credentials}"},
                data={"grant_type": "client_credentials"},
            ) as resp:
                data = await resp.json()
        _spotify_token = data.get("access_token")
        expires_in = data.get("expires_in", 3600)
        _spotify_token_expiry = time.monotonic() + expires_in - 60  # 60s safety margin
        print(f"[spotify_token] fetched new token, expires in {expires_in}s")
        return _spotify_token
    except Exception as e:
        print(f"[spotify_token] failed to fetch token: {e}")
        return None



def get_ffmpeg_options(normalize: bool = False) -> dict:
    """Return the appropriate FFmpeg options based on whether normalization is enabled."""
    return FFMPEG_OPTIONS_NORMALIZED if normalize else FFMPEG_OPTIONS

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
    - Otherwise, clean noise tags and build fallback attempts.
    """
    if query.startswith("http://") or query.startswith("https://"):
        return [query]

    # Unwrap ytsearch prefix so we work with the raw text
    raw = re.sub(r"^ytsearch\d+:", "", query).strip()

    # Strip known noise tags that confuse YouTube search (brackets are treated as
    # filter syntax, and labels like [Explicit] / (Official Video) add no signal).
    NOISE_RE = re.compile(
        r"[\[\(]"
        r"(?:explicit|clean|official|audio|video|music\s*video|mv|lyric(?:s)?|visualizer|hd|4k|remaster(?:ed)?|feat\.?|ft\.?)"
        r"[^\]\)]*"
        r"[\]\)]",
        re.IGNORECASE,
    )
    cleaned = NOISE_RE.sub("", raw).strip()
    # Also collapse any remaining empty brackets like [] or ()
    cleaned = re.sub(r"[\[\(]\s*[\]\)]", "", cleaned).strip()

    attempts = []

    # 1. Cleaned query (noise tags removed) — best signal for YouTube
    attempts.append(f"ytsearch5:{cleaned}")

    # 2. Raw original, if different (preserves any bracket content we didn't strip)
    if raw != cleaned:
        attempts.append(f"ytsearch5:{raw}")

    # 3. Strip ALL remaining parentheses/brackets as last resort
    bare = re.sub(r"[\(\[].*?[\)\]]", "", cleaned).strip()
    if bare and bare != cleaned:
        attempts.append(f"ytsearch5:{bare}")

    # 4. Drop anything after a pipe
    pipe_segment = re.split(r"\s*\|\s*", bare or cleaned)[0].strip()
    if pipe_segment and pipe_segment != (bare or cleaned):
        attempts.append(f"ytsearch5:{pipe_segment}")

    # Deduplicate while preserving order
    seen, unique = set(), []
    for a in attempts:
        if a not in seen:
            seen.add(a)
            unique.append(a)
    return unique

# ── Metadata fetch ────────────────────────────────────────────────────────────

async def fetch_spotify_track_meta(url: str) -> dict | None:
    """Fetch clean metadata + album art from Spotify API for a track URL."""
    try:
        match = re.search(r"spotify\.com/track/([A-Za-z0-9]+)", url)
        if not match:
            return None
        track_id = match.group(1)
        token = await _get_spotify_token(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)
        if not token:
            return None
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.spotify.com/v1/tracks/{track_id}",
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

        artists = [a["name"] for a in data.get("artists", [])]
        images  = data.get("album", {}).get("images", [])
        # Spotify returns images sorted largest first
        thumb   = images[0]["url"] if images else None
        return {
            "title":     data.get("name", "").strip(),
            "artist":    ", ".join(artists).strip(),
            "album":     data.get("album", {}).get("name", "").strip() or None,
            "duration":  data.get("duration_ms", 0) // 1000 or None,
            "thumbnail": thumb,
        }
    except Exception as e:
        print(f"[fetch_spotify_track_meta] failed: {e}")
        return None


# ── Embed builder ─────────────────────────────────────────────────────────────

async def build_now_playing_embed(
    meta: dict,
    queued_count: int = 0,
    spotify_url: str | None = None,
) -> tuple[discord.Embed, discord.File | None]:

    def fmt_duration(seconds):
        if not seconds:
            return "Unknown"
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"

    def _normalize_str(s: str) -> str:
        s = s.lower()
        s = re.sub(r"\(.*?\)|\[.*?\]", "", s)
        s = re.sub(r"[^a-z0-9\s]", "", s)
        return s.strip()

    def _matches(sp_meta: dict, dl_meta: dict) -> bool:
        sp_title  = _normalize_str(sp_meta.get("title", ""))
        sp_artist = _normalize_str(sp_meta.get("artist", ""))
        dl_title  = _normalize_str(dl_meta.get("title", ""))
        dl_artist = _normalize_str(dl_meta.get("artist", ""))
        title_ok  = sp_title and (sp_title in dl_title or dl_title in sp_title)
        artist_ok = sp_artist and any(
            a.strip() in dl_artist or dl_artist in a.strip()
            for a in sp_artist.split(",")
        )
        return title_ok or artist_ok

    def _build_composite(img_bytes: bytes) -> bytes:
        """
        Composite: blurred + desaturated background, sharp centered foreground.
        Returns PNG bytes.
        """
        from PIL import Image, ImageFilter, ImageEnhance
        import io as _io

        W, H = 1280, 720  # 16:9 canvas

        src = Image.open(_io.BytesIO(img_bytes)).convert("RGB")

        # ── Background: scale to fill, blur, desaturate ───────────────────────
        bg_scale = max(W / src.width, H / src.height)
        bg = src.resize(
            (int(src.width * bg_scale), int(src.height * bg_scale)),
            Image.LANCZOS,
        )
        # Center-crop to canvas
        bx = (bg.width  - W) // 2
        by = (bg.height - H) // 2
        bg = bg.crop((bx, by, bx + W, by + H))
        # Heavy blur
        bg = bg.filter(ImageFilter.GaussianBlur(radius=7)) # default 28
        # Desaturate (pull toward gray) — keep a touch of color like the screenshot
        bg = ImageEnhance.Color(bg).enhance(0.35)
        # Darken slightly so the foreground pops
        bg = ImageEnhance.Brightness(bg).enhance(0.6)

        # ── Foreground: fit inside center box, sharp ──────────────────────────
        box_h = int(H * 0.82)  # foreground takes ~82% of height
        scale = box_h / src.height
        fg_w  = int(src.width * scale)
        fg_h  = box_h
        fg = src.resize((fg_w, fg_h), Image.LANCZOS)

        # Paste centered
        px = (W - fg_w) // 2
        py = (H - fg_h) // 2
        bg.paste(fg, (px, py))

        out = _io.BytesIO()
        bg.save(out, format="PNG")
        return out.getvalue()

    async def _get_image_bytes(thumbnail) -> bytes | None:
        """Resolve thumbnail to raw bytes — handles both bytes and URL string."""
        if isinstance(thumbnail, bytes):
            return thumbnail
        if isinstance(thumbnail, str) and thumbnail.startswith("http"):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(thumbnail, timeout=aiohttp.ClientTimeout(total=8)) as r:
                        if r.status == 200:
                            return await r.read()
            except Exception as e:
                print(f"[embed] thumbnail fetch failed: {e}")
        return None

    # ── Try to enrich with Spotify metadata ──────────────────────────────────
    spotify_meta = None
    if spotify_url:
        spotify_meta = await fetch_spotify_track_meta(spotify_url)
        if spotify_meta:
            if _matches(spotify_meta, meta):
                print(f"[embed] Spotify metadata matched — using Spotify cover art")
                meta = {**meta, **spotify_meta}
            else:
                print(f"[embed] Spotify metadata did NOT match — using YT metadata")
                spotify_meta = None

    title     = meta.get("title")    or "Unknown Title"
    artist    = meta.get("artist")   or "Unknown Artist"
    album     = meta.get("album")
    duration  = meta.get("duration")
    thumbnail = meta.get("thumbnail")

    embed = discord.Embed(color=0x1DB954)
    embed.title = title
    embed.add_field(name="Artist",   value=artist,                 inline=True)
    embed.add_field(name="Duration", value=fmt_duration(duration), inline=True)
    if album:
        embed.add_field(name="Album", value=album, inline=True)
    if queued_count:
        embed.add_field(
            name="Up next",
            value=f"{queued_count} song{'s' if queued_count != 1 else ''}",
            inline=True,
        )
    embed.set_footer(text="Now Playing")

    # ── Build composite image ─────────────────────────────────────────────────
    file = None
    img_bytes = await _get_image_bytes(thumbnail)
    if img_bytes:
        try:
            composite = await asyncio.get_event_loop().run_in_executor(
                None, _build_composite, img_bytes
            )
            file = discord.File(io.BytesIO(composite), filename="cover.png")
            embed.set_image(url="attachment://cover.png")
        except Exception as e:
            print(f"[embed] composite failed, falling back to raw thumbnail: {e}")
            # Fallback: just use whatever we had
            if isinstance(thumbnail, bytes):
                file = discord.File(io.BytesIO(thumbnail), filename="cover.png")
                embed.set_image(url="attachment://cover.png")
            elif isinstance(thumbnail, str) and thumbnail.startswith("http"):
                embed.set_image(url=thumbnail)

    return embed, file

# ── Audio download for voice playback ─────────────────────────────────────────

async def search_and_download_audio(query: str) -> tuple[str, dict] | tuple[None, None]:
    """
    Try to download audio for `query`, falling back to progressively simpler
    searches if the exact query returns no results.
    """

    VARIANT_RE = re.compile(
        r"\bslowed\b|\breverb\b|\bnightcore\b|\bsped\s*up\b|\blofi\b|\blo[-\s]fi\b|\bsuper\s*slowed\b",
        re.IGNORECASE,
    )

    def _run(ydl_opts, q):
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(q, download=True)
            return _first_entry(info)

    def _pick_best_url(q: str, want_variant: bool) -> str | None:
        """
        Fetch top 5 results without downloading, return URL of best match.
        Trusts yt-dlp ranking but skips results that are the wrong variant class
        or clearly don't contain the song name from the query.
        """
        opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "extract_flat": True}
        search_q = re.sub(r"^ytsearch\d+:", "ytsearch5:", q)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(search_q, download=False)
        entries = (info or {}).get("entries") or []
        if not entries:
            return None

        # Extract meaningful words from the query to check title relevance.
        # We care most about the song title words (not artist, not noise).
        raw_query = re.sub(r"^ytsearch\d+:", "", q).strip().lower()
        # Remove noise words that appear in YouTube titles but not queries
        noise = {"official", "audio", "video", "music", "lyrics", "explicit",
                 "clean", "ft", "feat", "remastered", "hd", "4k", "visualizer"}
        query_words = [w for w in re.findall(r"\w+", raw_query) if w not in noise]

        def title_score(title: str) -> int:
            """How many query words appear in this title (case-insensitive)."""
            t = title.lower()
            return sum(1 for w in query_words if w in t)

        best_url, best_score = None, -1
        for entry in entries:
            title = entry.get("title") or ""
            is_variant = bool(VARIANT_RE.search(title))
            if want_variant != is_variant:
                continue
            score = title_score(title)
            if score > best_score:
                best_score = score
                best_url = entry.get("url") or entry.get("webpage_url")

        if best_url:
            print(f"[_pick_best_url] best score={best_score}/{len(query_words)}: {best_url}")
            return best_url

        # No entry matched variant class — fall back to highest-scoring overall
        best_url, best_score = None, -1
        for entry in entries:
            title = entry.get("title") or ""
            score = title_score(title)
            if score > best_score:
                best_score = score
                best_url = entry.get("url") or entry.get("webpage_url")

        print(f"[_pick_best_url] fallback score={best_score}/{len(query_words)}: {best_url}")
        return best_url or (entries[0].get("url") or entries[0].get("webpage_url"))

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
    want_variant = bool(VARIANT_RE.search(re.sub(r"^ytsearch\d+:", "", query).strip()))

    for attempt in search_attempts:
        print(f"[search_and_download_audio] trying: {attempt}")
        with tempfile.TemporaryDirectory() as tmpdir:
            ydl_opts = _make_ydl_opts(os.path.join(tmpdir, "%(title).50s.%(ext)s"))

            try:
                if attempt.startswith("ytsearch"):
                    # Fast path: peek at the top result first (no download).
                    # Only re-pick if the top result is a slowed/reverb variant we don't want.
                    best_url = await loop.run_in_executor(None, lambda: _pick_best_url(attempt, want_variant))
                    if not best_url:
                        continue
                    print(f"[search_and_download_audio] picked: {best_url}")
                    info = await loop.run_in_executor(None, lambda: _run(ydl_opts, best_url))
                else:
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

    artist, title, label = "", "", ""

    # ── 1. Spotify Web API — metadata only ───────────────────────────────────
    try:
        match = re.search(r"spotify\.com/track/([A-Za-z0-9]+)", url)
        if match:
            track_id = match.group(1)
            token = await _get_spotify_token(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)
            if token:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"https://api.spotify.com/v1/tracks/{track_id}",
                        headers={"Authorization": f"Bearer {token}"},
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            title   = data.get("name", "").strip()
                            artists = [a["name"] for a in data.get("artists", [])]
                            artist  = ", ".join(artists).strip()
                            label   = f"{artist} - {title}" if artist else title
                            print(f"[resolve_spotify] Spotify API ok — artist={artist!r} title={title!r}")
                        else:
                            text = await resp.text()
                            print(f"[resolve_spotify] Spotify API {resp.status}: {text[:200]}")
    except Exception as e:
        print(f"[resolve_spotify] Spotify API failed: {e}")

    # ── 2. song.link — platform URL + metadata fallback ──────────────────────
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.song.link/v1-alpha.1/links?url={url}&userCountry=US"
            ) as resp:
                sl_data = await resp.json()

        entities = list(sl_data.get("entitiesByUniqueId", {}).values())
        links    = sl_data.get("linksByPlatform", {})

        if entities and not label:
            # Only use song.link metadata if Spotify API didn't give us anything
            entity = entities[0]
            artist = entity.get("artistName", "").strip()
            title  = entity.get("title", "").strip()
            label  = f"{artist} - {title}" if artist else title
            print(f"[resolve_spotify] song.link metadata — artist={artist!r} title={title!r}")
        elif entities:
            print(f"[resolve_spotify] song.link ok — using Spotify API metadata, checking platform URLs")

        platform_priority = ["soundcloud", "youtubeMusic", "youtube"]
        for platform in platform_priority:
            platform_url = links.get(platform, {}).get("url")
            if platform_url:
                print(f"[resolve_spotify] using platform={platform!r} url={platform_url!r}")
                return platform_url, label

        if label:
            print(f"[resolve_spotify] no platform URL found, falling back to search")
            return f"ytsearch1:{label}", label

    except Exception as e:
        print(f"[resolve_spotify] song.link failed: {e}")

    # ── 3. Scrape Spotify og tags ─────────────────────────────────────────────
    if label:
        # Spotify API gave us metadata but song.link failed entirely — use what we have
        print(f"[resolve_spotify] song.link failed but have metadata, falling back to search")
        return f"ytsearch1:{label}", label

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                html = await resp.text()

        og_title = re.search(r'<meta property="og:title" content="([^"]+)"', html)
        og_desc  = re.search(r'<meta name="description" content="([^"]+)"', html)

        if og_title:
            raw_title = og_title.group(1).strip()
            raw_title = re.sub(r"(?i)^listen to (.+) on spotify$", r"\1", raw_title).strip()
            artist = ""
            if og_desc:
                parts = [p.strip() for p in og_desc.group(1).split("·")]
                if parts:
                    artist = parts[0]
            if " - " in raw_title:
                label = raw_title
            elif artist:
                label = f"{artist} - {raw_title}"
            else:
                label = raw_title

            print(f"[resolve_spotify] scraped og tags — label={label!r}")
            return f"ytsearch1:{label}", label

    except Exception as e:
        print(f"[resolve_spotify] og scrape failed: {e}")

    print(f"[resolve_spotify] all resolution attempts failed for {url}")
    return None, None


# ── Playlist / multi-track resolution ────────────────────────────────────────

def _is_spotify_url(url: str) -> bool:
    return "spotify.com" in url or "open.spotify.com" in url

def _is_apple_music_url(url: str) -> bool:
    return "music.apple.com" in url

def _is_youtube_url(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url

def _is_soundcloud_url(url: str) -> bool:
    return "soundcloud.com" in url

def _is_playlist_url(url: str) -> bool:
    """Return True if the URL looks like a playlist/album rather than a single track."""
    if _is_youtube_url(url):
        return "list=" in url and "watch?v=" not in url
    if _is_spotify_url(url):
        return "/playlist/" in url or "/album/" in url
    if _is_apple_music_url(url):
        return "/playlist/" in url or "/album/" in url
    if _is_soundcloud_url(url):
        return "/sets/" in url
    return False


async def resolve_apple_music_to_query(url: str) -> tuple[str, str] | tuple[None, None]:
    """Resolve an Apple Music track URL to a YouTube search query via song.link."""
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

        # Fallback: scrape og tags
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                html = await resp.text()
        og_title = re.search(r'<meta property="og:title" content="([^"]+)"', html)
        if og_title:
            raw = og_title.group(1).strip()
            return f"ytsearch1:{raw}", raw

    except Exception:
        pass

    return None, None


async def resolve_playlist_tracks(url: str) -> list[tuple[str, str]] | None:
    """
    Resolve a playlist/album URL to a list of (search_query, label) tuples.
    Returns None if the URL is not a recognised playlist type or resolution fails.
    Supports: Spotify playlists/albums, Apple Music playlists/albums,
              YouTube playlists, SoundCloud sets.
    """

    # ── YouTube playlist ──────────────────────────────────────────────────────
    if _is_youtube_url(url):
        def _yt_extract():
            opts = {
                "quiet": True,
                "no_warnings": True,
                "extract_flat": True,
                "skip_download": True,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        loop = asyncio.get_event_loop()
        try:
            info = await loop.run_in_executor(None, _yt_extract)
        except Exception as e:
            print(f"[resolve_playlist_tracks] YouTube error: {e}")
            return None

        entries = (info or {}).get("entries") or []
        tracks = []
        for entry in entries:
            if not entry:
                continue
            entry_url = entry.get("url") or entry.get("webpage_url") or entry.get("id")
            if entry.get("id") and not entry_url.startswith("http"):
                entry_url = f"https://www.youtube.com/watch?v={entry['id']}"
            title = entry.get("title") or entry_url
            if entry_url:
                tracks.append((entry_url, title))
        return tracks or None

    # ── SoundCloud set ────────────────────────────────────────────────────────
    if _is_soundcloud_url(url):
        def _sc_extract():
            opts = {
                "quiet": True,
                "no_warnings": True,
                "extract_flat": True,
                "skip_download": True,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        loop = asyncio.get_event_loop()
        try:
            info = await loop.run_in_executor(None, _sc_extract)
        except Exception as e:
            print(f"[resolve_playlist_tracks] SoundCloud error: {e}")
            return None

        entries = (info or {}).get("entries") or []
        tracks = []
        for entry in entries:
            if not entry:
                continue
            entry_url = entry.get("url") or entry.get("webpage_url")
            title = entry.get("title") or entry_url
            if entry_url:
                tracks.append((entry_url, title))
        return tracks or None

    # ── Spotify or Apple Music playlist/album ─────────────────────────────────
    if _is_spotify_url(url) or _is_apple_music_url(url):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://api.song.link/v1-alpha.1/links?url={url}&userCountry=US"
                ) as resp:
                    data = await resp.json()

            # song.link only resolves single tracks; for playlists we scrape the page
            # to get the track listing then resolve each via search.
            # Fall through to scraping below.
        except Exception:
            pass

        # Scrape the Spotify/Apple Music page for track titles
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    html = await resp.text()
        except Exception as e:
            print(f"[resolve_playlist_tracks] scrape error: {e}")
            return None

        tracks = []

        if _is_spotify_url(url):
            # Spotify embeds track data as JSON in a <script id="__NEXT_DATA__"> tag
            next_data = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
            if next_data:
                import json
                try:
                    obj = json.loads(next_data.group(1))
                    # Navigate to the track list — structure varies by playlist vs album
                    page_props = obj.get("props", {}).get("pageProps", {})
                    state = page_props.get("state", {})
                    entities = state.get("data", {}).get("entity", {})
                    items = (
                        entities.get("trackList")          # albums
                        or entities.get("tracks", {}).get("items", [])  # playlists
                        or []
                    )
                    for item in items:
                        track = item.get("track") or item  # playlists wrap in {track: ...}
                        t_name   = track.get("name") or track.get("title") or ""
                        artists  = track.get("artists") or track.get("artistsWithRoles") or []
                        if isinstance(artists, list) and artists:
                            artist_name = artists[0].get("profile", {}).get("name") or artists[0].get("name") or ""
                        else:
                            artist_name = ""
                        if t_name:
                            label = f"{artist_name} - {t_name}" if artist_name else t_name
                            query = f"ytsearch1:{artist_name} {t_name}".strip()
                            tracks.append((query, label))
                except Exception as e:
                    print(f"[resolve_playlist_tracks] Spotify JSON parse error: {e}")

            if not tracks:
                # Fallback: og:title often contains "Artist · Song" patterns on album pages
                og_titles = re.findall(r'"name"\s*:\s*"([^"]+)"', html)
                for t in og_titles[:50]:
                    tracks.append((f"ytsearch1:{t}", t))

        elif _is_apple_music_url(url):
            # Apple Music embeds structured data as JSON-LD
            json_ld = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
            if json_ld:
                import json
                try:
                    obj = json.loads(json_ld.group(1))
                    items = obj.get("track") or obj.get("tracks") or []
                    for item in items:
                        t_name = item.get("name") or ""
                        artist_name = ""
                        by_artist = item.get("byArtist")
                        if isinstance(by_artist, dict):
                            artist_name = by_artist.get("name") or ""
                        elif isinstance(by_artist, list) and by_artist:
                            artist_name = by_artist[0].get("name") or ""
                        if t_name:
                            label = f"{artist_name} - {t_name}" if artist_name else t_name
                            query = f"ytsearch1:{artist_name} {t_name}".strip()
                            tracks.append((query, label))
                except Exception as e:
                    print(f"[resolve_playlist_tracks] Apple Music JSON-LD parse error: {e}")

        return tracks or None

    return None


# ── Voice playback ────────────────────────────────────────────────────────────

def _cancel_autoplay(guild_id: int):
    state = voice_states.get(guild_id)
    if state and state.get("autoplay_task"):
        # autoplay_task is now always an asyncio.Task (via _schedule_autoplay_task),
        # so .cancel() correctly injects CancelledError even mid-sleep.
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
    normalize = meta.get("normalize", False)
    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(filepath, **get_ffmpeg_options(normalize)),
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
        # Use asyncio.ensure_future so we get a real asyncio.Task whose .cancel()
        # injects CancelledError into the coroutine even after it has started sleeping.
        # run_coroutine_threadsafe returns a concurrent.futures.Future whose .cancel()
        # only works before the event loop picks it up — useless once the sleep begins.
        task = asyncio.run_coroutine_threadsafe(
            _schedule_autoplay_task(guild_id, vc, bot),
            bot.loop,
        )
        state["autoplay_task"] = task
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
    normalize = meta.get("normalize", False)
    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(filepath, **get_ffmpeg_options(normalize)),
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

    if not silent:
        # Send Now Playing embed to text channel
        async def _send_now_playing():
            channel = bot.get_channel(state.get("text_channel_id"))
            if not channel:
                return
            embed, file = await build_now_playing_embed(meta, queued_count=len(state["queue"]), spotify_url=meta.get("spotify_url"))
            if file:
                await channel.send(embed=embed, file=file)
            else:
                await channel.send(embed=embed)

        asyncio.run_coroutine_threadsafe(_send_now_playing(), bot.loop)


async def _schedule_autoplay_task(guild_id: int, vc: discord.VoiceClient, bot: discord.Client):
    """
    Runs on the bot's event loop. Creates a real asyncio.Task for autoplay so that
    _cancel_autoplay can properly inject CancelledError into a sleeping coroutine.
    The concurrent.futures.Future returned by run_coroutine_threadsafe can only be
    cancelled before the event loop starts it — this wrapper solves that.
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
        normalize = state.get("normalize", False)
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(filepath, **get_ffmpeg_options(normalize)),
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