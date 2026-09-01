"""
ytembed_server.server -- fxtwitter-style link-rewrite proxy for YouTube.

Runs as its own docker-compose service (see the `ytembed` entry in
docker-compose.yml, and Dockerfile in this directory), reachable publicly at
whatever subdomain you point at it (VIDEO_EMBED_DOMAIN) -- unlike
intent_server/gpu_worker, this one is meant to be internet-facing, since the
whole point is that link-preview crawlers (Discord, Telegram, etc.) hit it
directly when someone pastes a rewritten link in a chat.

The trick (same one fxtwitter/vxtwitter use): a real YouTube URL like
    https://www.youtube.com/watch?v=VIDEO_ID
becomes
    https://<VIDEO_EMBED_DOMAIN>/watch?v=VIDEO_ID
Paste the second one anywhere. A real browser opening it gets redirected
straight to the real YouTube URL -- nothing changes for a human clicking it.
A link-preview crawler (identified by User-Agent) instead gets an HTML page
with OpenGraph/Twitter-card `video` meta tags pointing at a locally-cached
mp4, so the *crawler's own platform* renders an inline, auto-playing video
embed -- no Discord bot integration involved on this end at all.

Endpoints:
    GET  /health              -- liveness check
    GET  /watch?v=ID          -- YouTube "watch" page equivalent
    GET  /shorts/{id}         -- YouTube Shorts equivalent
    GET  /{id}                -- bare-ID catch-all, for youtu.be-style links
    GET  /media/{id}.mp4      -- serves the cached video file (og:video target)
    GET  /player/{id}         -- minimal <video> page (twitter:player target)

Config (env vars):
    VIDEO_EMBED_DOMAIN          -- required. Public domain this service is
                                   reachable at (e.g. "yt.example.dev"), used
                                   to build absolute og:video/player URLs.
                                   Mirrored in app/core/config.py as a
                                   documented constant of the same name --
                                   that copy isn't imported here (this folder
                                   stays self-contained, same as
                                   gpu_worker/intent_server), so the two must
                                   be kept in sync by hand.
    VIDEO_EMBED_PORT            -- defaults to 8804.
    VIDEO_EMBED_CACHE_DIR       -- defaults to "cache" (relative to this file).
    VIDEO_EMBED_MAX_MB          -- max cached file size; downloads that would
                                   exceed it are discarded and the request
                                   falls back to a redirect. Defaults to 50.
    VIDEO_EMBED_CACHE_TTL_SECONDS -- how long a cached video is kept before
                                   the cleanup loop deletes it. Defaults to
                                   6 hours.

This file deliberately does NOT talk to Discord or the bot in any way -- it
is a plain website. Wiring a chat platform to actually rewrite links to this
domain is a separate, not-yet-built step.
"""

import asyncio
import glob
import html
import json
import os
import re
import subprocess
import time
from collections import defaultdict

import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

VIDEO_EMBED_DOMAIN = os.environ.get("VIDEO_EMBED_DOMAIN", "")
PORT                = int(os.environ.get("VIDEO_EMBED_PORT", "8804"))
CACHE_DIR           = os.environ.get(
    "VIDEO_EMBED_CACHE_DIR", os.path.join(os.path.dirname(__file__), "cache")
)
MAX_MB              = float(os.environ.get("VIDEO_EMBED_MAX_MB", "50"))
MAX_BYTES           = int(MAX_MB * 1024 * 1024)
CACHE_TTL_SECONDS   = int(os.environ.get("VIDEO_EMBED_CACHE_TTL_SECONDS", str(6 * 3600)))
CLEANUP_INTERVAL_SECONDS = 30 * 60

# Same spoofed UA as the main bot's downloader (core/config.py's
# YTDLP_USER_AGENT) -- yt-dlp's default UA gets blocked/degraded by some
# sites, YouTube included on occasion.
YTDLP_USER_AGENT = os.environ.get(
    "YTDLP_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
)

# Known link-preview/unfurler bots. Substring match against a lowercased
# User-Agent header. Add more here as you notice a platform not embedding --
# anything NOT on this list is treated as a real human browser and just
# redirected straight to the real YouTube URL, so it's safe to be generous
# about what counts as a crawler.
CRAWLER_UA_SUBSTRINGS = [
    "discordbot", "telegrambot", "twitterbot", "facebookexternalhit",
    "whatsapp", "slackbot", "redditbot", "linkedinbot", "skypeuripreview",
    "vkshare", "embedly", "quora link preview", "iframely", "w3c_validator",
    "google-inspectiontool", "pinterest", "tumblr", "bot/", "bot ",
]

# Standard YouTube video IDs are 11 chars, but this stays a bit loose rather
# than hardcoding exactly 11 -- it only needs to reject path-traversal/junk,
# not validate real YouTube ID shape (yt-dlp does that when it hits the URL).
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")

app = FastAPI()
_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def _is_crawler(user_agent: str) -> bool:
    ua = user_agent.lower()
    return any(s in ua for s in CRAWLER_UA_SUBSTRINGS)


def _real_youtube_url(video_id: str, kind: str) -> str:
    if kind == "shorts":
        return f"https://www.youtube.com/shorts/{video_id}"
    return f"https://www.youtube.com/watch?v={video_id}"


def _probe_dimensions(filepath: str) -> tuple[int, int, float]:
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", filepath,
        ], timeout=30)
        info = json.loads(out)
        duration = float(info["format"].get("duration", 0))
        width = height = 0
        for s in info.get("streams", []):
            if s.get("codec_type") == "video":
                width, height = int(s.get("width", 0)), int(s.get("height", 0))
                break
        return width, height, duration
    except Exception:
        return 0, 0, 0.0


def _download_sync(video_id: str, url: str) -> dict | None:
    """
    Blocking. Runs in a thread executor -- called only while holding
    `_locks[video_id]`, so two crawlers hitting the same fresh link at once
    don't trigger two downloads.
    """
    outtmpl = os.path.join(CACHE_DIR, f"{video_id}.%(ext)s")
    opts = {
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "http_headers": {"User-Agent": YTDLP_USER_AGENT},
        # Capped at 720p and MAX_BYTES -- this is for link-preview embeds,
        # not archival quality. yt-dlp's filesize filter is best-effort
        # (some formats don't report it), so the actual size is re-checked
        # after download below regardless.
        "format": (
            f"best[ext=mp4][height<=720][filesize<{MAX_BYTES}]"
            f"/best[height<=720][filesize<{MAX_BYTES}]"
            f"/best[height<=720]"
            f"/best"
        ),
        "merge_output_format": "mp4",
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        print(f"[ytembed] download failed for {video_id}: {e}", flush=True)
        return None

    candidates = [
        f for f in glob.glob(os.path.join(CACHE_DIR, f"{video_id}.*"))
        if not f.endswith(".json")
    ]
    if not candidates:
        return None
    filepath = max(candidates, key=os.path.getsize)

    if os.path.getsize(filepath) > MAX_BYTES:
        os.remove(filepath)
        return None

    final_path = os.path.join(CACHE_DIR, f"{video_id}.mp4")
    if filepath != final_path:
        os.replace(filepath, final_path)

    width, height, duration = _probe_dimensions(final_path)

    meta = {
        "title": info.get("title") or video_id,
        "uploader": info.get("uploader") or "",
        "thumbnail": info.get("thumbnail") or "",
        "width": width or 1280,
        "height": height or 720,
        "duration": duration,
        "cached_at": time.time(),
    }
    with open(os.path.join(CACHE_DIR, f"{video_id}.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    return meta


async def _get_or_download(video_id: str, real_url: str) -> dict | None:
    meta_path = os.path.join(CACHE_DIR, f"{video_id}.json")
    file_path = os.path.join(CACHE_DIR, f"{video_id}.mp4")

    def _read_cached() -> dict | None:
        if os.path.exists(meta_path) and os.path.exists(file_path):
            try:
                with open(meta_path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return None
        return None

    cached = _read_cached()
    if cached:
        return cached

    async with _locks[video_id]:
        # Re-check -- another request may have finished the download while
        # this one was waiting on the lock.
        cached = _read_cached()
        if cached:
            return cached
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: _download_sync(video_id, real_url))


def _render_embed_page(video_id: str, meta: dict) -> str:
    base        = f"https://{VIDEO_EMBED_DOMAIN}"
    video_url   = f"{base}/media/{video_id}.mp4"
    player_url  = f"{base}/player/{video_id}"
    watch_url   = f"{base}/watch?v={video_id}"
    title       = html.escape(meta["title"])
    uploader    = html.escape(meta["uploader"] or "video")
    thumb       = html.escape(meta.get("thumbnail") or "")
    width       = meta.get("width") or 1280
    height      = meta.get("height") or 720

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta property="og:type" content="video.other">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{uploader}">
<meta property="og:url" content="{watch_url}">
<meta property="og:site_name" content="{html.escape(VIDEO_EMBED_DOMAIN)}">
<meta property="og:image" content="{thumb}">
<meta property="og:video" content="{video_url}">
<meta property="og:video:secure_url" content="{video_url}">
<meta property="og:video:type" content="video/mp4">
<meta property="og:video:width" content="{width}">
<meta property="og:video:height" content="{height}">
<meta name="twitter:card" content="player">
<meta name="twitter:title" content="{title}">
<meta name="twitter:player" content="{player_url}">
<meta name="twitter:player:width" content="{width}">
<meta name="twitter:player:height" content="{height}">
<meta name="twitter:player:stream" content="{video_url}">
<title>{title}</title>
</head>
<body></body>
</html>"""


async def _handle(video_id: str, kind: str, request: Request):
    if not VIDEO_ID_RE.match(video_id):
        raise HTTPException(status_code=404, detail="not found")

    real_url = _real_youtube_url(video_id, kind)

    if not _is_crawler(request.headers.get("user-agent", "")):
        return RedirectResponse(real_url, status_code=302)

    meta = await _get_or_download(video_id, real_url)
    if not meta:
        # Download failed or came out too large -- fall back to the real
        # YouTube URL so the crawler at least gets YouTube's own preview
        # instead of a dead link.
        return RedirectResponse(real_url, status_code=302)

    return HTMLResponse(_render_embed_page(video_id, meta))


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/watch")
async def watch(request: Request, v: str = ""):
    return await _handle(v, "watch", request)


@app.get("/shorts/{video_id}")
async def shorts(video_id: str, request: Request):
    return await _handle(video_id, "shorts", request)


@app.get("/media/{video_id}.mp4")
async def media(video_id: str):
    if not VIDEO_ID_RE.match(video_id):
        raise HTTPException(status_code=404, detail="not found")
    file_path = os.path.join(CACHE_DIR, f"{video_id}.mp4")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="not cached -- fetch /watch?v=<id> first")
    return FileResponse(file_path, media_type="video/mp4")


@app.get("/player/{video_id}")
async def player(video_id: str):
    if not VIDEO_ID_RE.match(video_id):
        raise HTTPException(status_code=404, detail="not found")
    video_url = f"https://{VIDEO_EMBED_DOMAIN}/media/{video_id}.mp4"
    return HTMLResponse(
        f'<!doctype html><html><body style="margin:0;background:#000">'
        f'<video src="{video_url}" controls autoplay muted loop '
        f'style="width:100%;height:100%"></video></body></html>'
    )


@app.get("/{video_id}")
async def bare(video_id: str, request: Request):
    """youtu.be-style bare short links (e.g. yourdomain.dev/dQw4w9WgXcQ)."""
    return await _handle(video_id, "watch", request)


async def _cleanup_loop():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        now = time.time()
        for meta_path in glob.glob(os.path.join(CACHE_DIR, "*.json")):
            try:
                with open(meta_path, encoding="utf-8") as f:
                    meta = json.load(f)
                if now - meta.get("cached_at", 0) <= CACHE_TTL_SECONDS:
                    continue
                video_id = os.path.splitext(os.path.basename(meta_path))[0]
                for ext in (".json", ".mp4"):
                    p = os.path.join(CACHE_DIR, video_id + ext)
                    if os.path.exists(p):
                        os.remove(p)
            except Exception:
                continue


@app.on_event("startup")
async def _startup():
    os.makedirs(CACHE_DIR, exist_ok=True)
    asyncio.create_task(_cleanup_loop())
    print(f"[ytembed] ready -- domain={VIDEO_EMBED_DOMAIN or '(unset!)'} cache={CACHE_DIR}", flush=True)


if __name__ == "__main__":
    if not VIDEO_EMBED_DOMAIN:
        raise SystemExit("VIDEO_EMBED_DOMAIN must be set")
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
