import re
import time
import aiohttp
from config import SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET


# ── Token cache ───────────────────────────────────────────────────────────────

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
        _spotify_token_expiry = time.monotonic() + expires_in - 60
        print(f"[spotify_token] fetched new token, expires in {expires_in}s")
        return _spotify_token
    except Exception as e:
        print(f"[spotify_token] failed to fetch token: {e}")
        return None


# ── Track metadata ────────────────────────────────────────────────────────────

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


# ── Track resolution (Spotify URL → YouTube/search query) ─────────────────────

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