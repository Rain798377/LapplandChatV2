import os
import re
import sys
import glob
import json
import shutil
import asyncio
import tempfile
import subprocess
import functools
import yt_dlp
from .utils import _first_entry, _build_search_attempts


VARIANT_RE = re.compile(
    r"\bslowed\b|\breverb\b|\bnightcore\b|\bsped\s*up\b|\blofi\b|\blo[-\s]fi\b|\bsuper\s*slowed\b",
    re.IGNORECASE,
)

# Cache: (query_lower, want_variant, expected_title_lower, expected_artist_lower) → best URL
# Prevents non-deterministic YTMusic results from returning a different video on each call.
_url_cache: dict[tuple, str] = {}


def _ytmusic_search_entries(query_text: str, n: int = 5) -> list[dict]:
    """
    Search YouTube Music and return up to `n` entries whose URLs are on
    music.youtube.com — plain youtube.com videos are excluded so we only
    get proper YTMusic catalogue results (songs, not user-uploaded videos).

    Fetches 3x the requested count before filtering to absorb plain-YT results
    and give the scorer more options to work with.
    """
    import urllib.parse
    search_url = (
        "https://music.youtube.com/search?q="
        + urllib.parse.quote_plus(query_text)
    )
    fetch_n = n * 3
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": True,
        "playlist_items": f"1:{fetch_n}",
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(search_url, download=False)
        entries = (info or {}).get("entries") or []

        # Flatten one level — YTMusic search returns section playlists
        flat = []
        for e in entries:
            if e and e.get("entries"):
                flat.extend(e["entries"])
            elif e:
                flat.append(e)

        # Keep only music.youtube.com results; skip bare youtube.com videos.
        music_only = []
        for e in flat:
            if not e:
                continue
            webpage = e.get("webpage_url") or ""
            if webpage and "music.youtube.com" not in webpage and "youtube.com/watch" in webpage:
                print(f"[_ytmusic_search_entries] skipping plain YT video: {e.get('title')!r}")
                continue
            music_only.append(e)
            if len(music_only) >= n:
                break

        print(f"[_ytmusic_search_entries] {len(music_only)} music.youtube.com results for {query_text!r}")
        return music_only
    except Exception as exc:
        print(f"[_ytmusic_search_entries] failed: {exc}")
        return []


def _normalize_meta(s: str) -> str:
    """Lowercase, strip bracketed suffixes and punctuation for fuzzy comparison."""
    s = s.lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", "", s)   # drop (feat. ...), [official], etc.
    s = re.sub(r"[^\w\s]", "", s)            # drop punctuation
    return s.strip()


def _meta_match_bonus(
    entry_title: str,
    entry_artist: str,
    expected_title: str,
    expected_artist: str,
) -> int:
    """
    Return a score bonus based on how well the entry's title/artist match
    the expected (e.g. Spotify) metadata.

    Scoring:
      +6  exact normalised title match
      +3  expected title is a substring of entry title (or vice-versa)
      +4  exact normalised artist match
      +2  any expected artist token appears in entry artist
      ─────────────────────────────────────────────────────
      max +10 (title) + +6 (artist) = +16
    """
    bonus = 0
    if not (expected_title or expected_artist):
        return 0

    if expected_title:
        et = _normalize_meta(expected_title)
        dt = _normalize_meta(entry_title)
        if et and dt:
            if et == dt:
                bonus += 6
            elif et in dt or dt in et:
                bonus += 3

    if expected_artist:
        ea = _normalize_meta(expected_artist)
        da = _normalize_meta(entry_artist)
        if ea and da:
            if ea == da:
                bonus += 4
            else:
                # Handle "Artist1, Artist2" — any token match counts
                for tok in re.split(r"[,&]+", ea):
                    tok = tok.strip()
                    if tok and tok in da:
                        bonus += 2
                        break

    return bonus


# Titles containing these words are penalised unless the query also contains them.
_LIVE_RE = re.compile(
    r"\blive\b|\bconcert\b|\btour\b|\bperformance\b|\bunplugged\b|\bacoustic\b"
    r"|\bkaraoke\b|\binstrumental\b|\bcover\b|\btribute\b",
    re.IGNORECASE,
)


def _pick_best_url(
    q: str,
    want_variant: bool,
    expected_title: str = "",
    expected_artist: str = "",
) -> str | None:
    MAX_DURATION_SECONDS = 30 * 60  # 30 minutes

    raw_query = re.sub(r"^(?:ytsearch|ytmsearch)\d*:", "", q).strip()

    # ── Cache lookup ──────────────────────────────────────────────────────────
    cache_key = (
        raw_query.lower(),
        want_variant,
        expected_title.lower(),
        expected_artist.lower(),
    )
    if cache_key in _url_cache:
        cached = _url_cache[cache_key]
        print(f"[_pick_best_url] cache hit → {cached}")
        return cached

    # ── Collect candidates: YTMusic (music.youtube.com only) + regular YT ────
    yt_music_entries = _ytmusic_search_entries(raw_query, n=5)

    yt_opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "extract_flat": True}
    search_q = re.sub(r"^(?:ytsearch|ytmsearch)\d*:", "ytsearch5:", q)
    try:
        with yt_dlp.YoutubeDL(yt_opts) as ydl:
            yt_info = ydl.extract_info(search_q, download=False)
        yt_entries = (yt_info or {}).get("entries") or []
    except Exception as exc:
        print(f"[_pick_best_url] ytsearch failed: {exc}")
        yt_entries = []

    # (entry, from_ytmusic)
    entries_with_source = (
        [(e, True)  for e in yt_music_entries if e] +
        [(e, False) for e in yt_entries       if e]
    )

    if not entries_with_source:
        return None

    raw_lower = raw_query.lower()
    noise = {"official", "audio", "video", "music", "lyrics", "explicit",
             "clean", "ft", "feat", "remastered", "hd", "4k", "visualizer"}

    ascii_words  = [w for w in re.findall(r"[a-z0-9]+", raw_lower) if w not in noise]
    unicode_toks = re.findall(r"[^\x00-\x7F\s]+", raw_lower)
    query_tokens = ascii_words + unicode_toks

    query_wants_live = bool(_LIVE_RE.search(raw_lower))

    def title_score(entry: dict, from_ytmusic: bool) -> int:
        title  = entry.get("title")  or ""
        artist = entry.get("artist") or entry.get("uploader") or entry.get("channel") or ""
        t = title.lower()

        score = sum(1 for w in query_tokens if w in t)
        # CJK exact-match bonus
        score += sum(2 for tok in unicode_toks if tok in t)
        # Penalise live/cover/etc. when not wanted
        if not query_wants_live and _LIVE_RE.search(title):
            score -= 4
        # YTMusic entries are more likely official studio versions
        if from_ytmusic:
            score += 1
        # Metadata match bonus (Spotify title/artist vs yt-dlp entry fields)
        score += _meta_match_bonus(title, artist, expected_title, expected_artist)
        return score

    best_url, best_score = None, -1
    for entry, from_ytm in entries_with_source:
        duration = entry.get("duration") or 0
        if duration and duration > MAX_DURATION_SECONDS:
            continue
        title = entry.get("title") or ""
        is_variant = bool(VARIANT_RE.search(title))
        if want_variant != is_variant:
            continue
        score = title_score(entry, from_ytm)
        url   = entry.get("url") or entry.get("webpage_url")
        if score > best_score and url:
            best_score = score
            best_url   = url
            print(f"[_pick_best_url] candidate score={score} ytm={from_ytm} title={title!r}")

    if best_url:
        print(f"[_pick_best_url] best score={best_score}/{len(query_tokens)}: {best_url}")
        _url_cache[cache_key] = best_url
        return best_url

    # No entry matched variant class — fall back to highest-scoring overall
    best_url, best_score = None, -1
    for entry, from_ytm in entries_with_source:
        duration = entry.get("duration") or 0
        if duration and duration > MAX_DURATION_SECONDS:
            continue
        url   = entry.get("url") or entry.get("webpage_url")
        score = title_score(entry, from_ytm)
        if score > best_score and url:
            best_score = score
            best_url   = url

    print(f"[_pick_best_url] fallback score={best_score}/{len(query_tokens)}: {best_url}")
    first = entries_with_source[0][0]
    result = best_url or (first.get("url") or first.get("webpage_url"))
    if result:
        _url_cache[cache_key] = result
    return result


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


def _run_spotdl(url: str, outdir: str) -> tuple[list[str], str]:
    clean_url = url.split("?")[0] if "spotify.com" in url else url

    _spotdl_cfg = os.path.join(os.path.expanduser("~"), ".spotdl", "config.json")
    try:
        with open(_spotdl_cfg) as _f:
            _cfg = json.load(_f)
        if _cfg.get("load_config", False):
            _cfg["load_config"] = False
            with open(_spotdl_cfg, "w") as _f:
                json.dump(_cfg, _f, indent=4)
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

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=outdir)
    files = sorted(glob.glob(os.path.join(outdir, "**", "*.mp3"), recursive=True))

    combined = "\n".join(filter(None, [result.stdout.strip(), result.stderr.strip()]))
    combined += f"\n[exit code: {result.returncode}]"

    return files, combined


async def search_and_download_audio(
    query: str,
    expected_title: str = "",
    expected_artist: str = "",
) -> tuple[str, dict] | tuple[None, None]:
    """
    Download audio for `query`.
    - Spotify URLs  → spotdl
    - Everything else → yt-dlp search with progressive fallbacks

    `expected_title` and `expected_artist` (e.g. from Spotify metadata) are
    forwarded to _pick_best_url where they boost results whose yt-dlp metadata
    closely matches, helping avoid wrong versions/covers/live recordings.
    """

    # ── Spotify URL → spotdl (or yt-dlp for non-ASCII titles) ───────────────
    if "spotify.com" in query:
        loop = asyncio.get_event_loop()

        # Peek at Spotify metadata to detect non-ASCII titles before running spotdl.
        # spotdl fails on CJK / Arabic / etc. so we fall through to yt-dlp instead.
        _use_spotdl = True
        _sp_meta = None
        try:
            from .spotify_api import fetch_spotify_track_meta
            _sp_meta = await fetch_spotify_track_meta(query)
            if _sp_meta:
                _combined = f"{_sp_meta.get('title', '')} {_sp_meta.get('artist', '')}"
                if re.search(r"[^\x00-\x7F]", _combined):
                    _use_spotdl = False
                    print(f"[search_and_download_audio] non-ASCII in '{_combined}' — skipping spotdl, using yt-dlp")
        except Exception as e:
            print(f"[search_and_download_audio] metadata peek failed: {e}")

        if not _use_spotdl and _sp_meta:
            _title  = _sp_meta.get("title", "")
            _artist = _sp_meta.get("artist", "")
            _label  = f"{_artist} - {_title}".strip(" -")
            return await search_and_download_audio(
                f"ytsearch1:{_label}",
                expected_title=_title,
                expected_artist=_artist,
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                files, output = await loop.run_in_executor(
                    None, lambda: _run_spotdl(query, tmpdir)
                )
            except Exception as e:
                print(f"[search_and_download_audio] spotdl exception: {e} — falling back to yt-dlp")
                files, output = [], str(e)

            if not files:
                print(f"[search_and_download_audio] spotdl returned no files — falling back to yt-dlp:\n{output}")
                _title  = (_sp_meta or {}).get("title", "")
                _artist = (_sp_meta or {}).get("artist", "")
                _label  = f"{_artist} - {_title}".strip(" -") if (_title or _artist) else ""
                if not _label:
                    return None, None
                return await search_and_download_audio(
                    f"ytsearch1:{_label}",
                    expected_title=_title,
                    expected_artist=_artist,
                )

            filepath = files[0]
            display_name = os.path.basename(filepath)

            meta: dict = {
                "title": None, "artist": None, "album": None,
                "duration": None, "thumbnail": None,
                "spotify_url": query,
            }
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
            except Exception as e:
                print(f"[search_and_download_audio] mutagen failed: {e}")

            # Filename fallback: "Artist - Title.mp3"
            if not meta["title"]:
                stem = os.path.splitext(display_name)[0]
                if " - " in stem:
                    artist_part, title_part = stem.split(" - ", 1)
                    meta["artist"] = meta["artist"] or artist_part.strip()
                    meta["title"]  = title_part.strip()
                else:
                    meta["title"] = stem

            safe_name = re.sub(r'[<>:"/\\|?*]', '_', display_name).strip() or "audio.mp3"
            dest = os.path.join(tempfile.gettempdir(), safe_name)
            shutil.copy2(filepath, dest)

        if not os.path.exists(dest) or os.path.getsize(dest) == 0:
            print(f"[search_and_download_audio] spotdl dest missing or empty: {dest}")
            return None, None

        print(f"[search_and_download_audio] spotdl saved: {dest} ({os.path.getsize(dest)/1024:.1f}KB)")
        return dest, meta

    # ── Everything else → yt-dlp search ─────────────────────────────────────

    def _run(ydl_opts, q):
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(q, download=True)
            return _first_entry(info)

    loop = asyncio.get_event_loop()
    search_attempts = _build_search_attempts(query)
    want_variant = bool(VARIANT_RE.search(re.sub(r"^ytsearch\d+:", "", query).strip()))

    for attempt in search_attempts:
        # Capture loop variable by value — lambdas share the same closure cell
        # and would otherwise all reference the final iteration's value.
        _attempt = attempt

        print(f"[search_and_download_audio] trying: {_attempt}")
        with tempfile.TemporaryDirectory() as tmpdir:
            ydl_opts = _make_ydl_opts(os.path.join(tmpdir, "%(title).50s.%(ext)s"))

            try:
                if _attempt.startswith("ytsearch") or _attempt.startswith("ytmsearch"):
                    best_url = await loop.run_in_executor(
                        None,
                        lambda a=_attempt: _pick_best_url(
                            a, want_variant,
                            expected_title=expected_title,
                            expected_artist=expected_artist,
                        ),
                    )
                    if not best_url:
                        continue
                    print(f"[search_and_download_audio] picked: {best_url}")
                    info = await loop.run_in_executor(
                        None, lambda u=best_url: _run(ydl_opts, u)
                    )
                else:
                    info = await loop.run_in_executor(
                        None, lambda a=_attempt: _run(ydl_opts, a)
                    )
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