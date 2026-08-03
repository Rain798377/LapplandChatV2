import re
import asyncio
import aiohttp
import yt_dlp


# ── URL type detection ────────────────────────────────────────────────────────

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


# ── Apple Music resolution ────────────────────────────────────────────────────

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


# ── Playlist resolution ───────────────────────────────────────────────────────

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
        except Exception:
            pass

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    html = await resp.text()
        except Exception as e:
            print(f"[resolve_playlist_tracks] scrape error: {e}")
            return None

        tracks = []

        if _is_spotify_url(url):
            next_data = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
            if next_data:
                import json
                try:
                    obj = json.loads(next_data.group(1))
                    page_props = obj.get("props", {}).get("pageProps", {})
                    state = page_props.get("state", {})
                    entities = state.get("data", {}).get("entity", {})
                    items = (
                        entities.get("trackList")
                        or entities.get("tracks", {}).get("items", [])
                        or []
                    )
                    for item in items:
                        track = item.get("track") or item
                        t_name      = track.get("name") or track.get("title") or ""
                        artists     = track.get("artists") or track.get("artistsWithRoles") or []
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
                og_titles = re.findall(r'"name"\s*:\s*"([^"]+)"', html)
                for t in og_titles[:50]:
                    tracks.append((f"ytsearch1:{t}", t))

        elif _is_apple_music_url(url):
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