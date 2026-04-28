import os
import re
import glob
import shutil
import asyncio
import tempfile
import yt_dlp
from .utils import _first_entry, _build_search_attempts


VARIANT_RE = re.compile(
    r"\bslowed\b|\breverb\b|\bnightcore\b|\bsped\s*up\b|\blofi\b|\blo[-\s]fi\b|\bsuper\s*slowed\b",
    re.IGNORECASE,
)


def _pick_best_url(q: str, want_variant: bool) -> str | None:
    MAX_DURATION_SECONDS = 30 * 60  # 30 minutes
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "extract_flat": True}
    search_q = re.sub(r"^ytsearch\d+:", "ytsearch5:", q)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(search_q, download=False)
    entries = (info or {}).get("entries") or []
    if not entries:
        return None

    raw_query = re.sub(r"^ytsearch\d+:", "", q).strip().lower()
    noise = {"official", "audio", "video", "music", "lyrics", "explicit",
             "clean", "ft", "feat", "remastered", "hd", "4k", "visualizer"}
    query_words = [w for w in re.findall(r"\w+", raw_query) if w not in noise]

    def title_score(title: str) -> int:
        t = title.lower()
        return sum(1 for w in query_words if w in t)

    best_url, best_score = None, -1
    for entry in entries:
        title = entry.get("title") or ""
        duration = entry.get("duration") or 0
        if duration and duration > MAX_DURATION_SECONDS:
            continue
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
        duration = entry.get("duration") or 0
        if duration and duration > MAX_DURATION_SECONDS:
            continue
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
        "match_filter": yt_dlp.utils.match_filter_func("duration <= 1800"),
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


async def search_and_download_audio(query: str) -> tuple[str, dict] | tuple[None, None]:
    """
    Try to download audio for `query`, falling back to progressively simpler
    searches if the exact query returns no results.
    """

    def _run(ydl_opts, q):
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(q, download=True)
            return _first_entry(info)

    loop = asyncio.get_event_loop()
    search_attempts = _build_search_attempts(query)
    want_variant = bool(VARIANT_RE.search(re.sub(r"^ytsearch\d+:", "", query).strip()))

    for attempt in search_attempts:
        print(f"[search_and_download_audio] trying: {attempt}")
        with tempfile.TemporaryDirectory() as tmpdir:
            ydl_opts = _make_ydl_opts(os.path.join(tmpdir, "%(title).50s.%(ext)s"))

            try:
                if attempt.startswith("ytsearch"):
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
                    meta["thumbnail"] = apic.data
            except Exception:
                pass

            # yt-dlp fallback for anything missing
            if not meta["title"]:    meta["title"]    = info.get("title")
            if not meta["artist"]:   meta["artist"]   = info.get("uploader") or info.get("channel")
            if not meta["duration"]: meta["duration"] = info.get("duration")
            if meta["thumbnail"] is None:
                meta["thumbnail"] = info.get("thumbnail")

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