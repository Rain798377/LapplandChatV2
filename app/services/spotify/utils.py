import os
import re
import asyncio
import yt_dlp


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
    - Otherwise, build attempts in priority order.

    Note: _pick_best_url already queries YouTube Music internally for every
    attempt, so we only need ytsearch variants here as the outer retry loop.
    """
    if query.startswith("http://") or query.startswith("https://"):
        return [query]

    raw = re.sub(r"^(?:ytsearch|ytmsearch)\d*:", "", query).strip()

    NOISE_RE = re.compile(
        r"[\[\(]"
        r"(?:explicit|clean|official|audio|video|music\s*video|mv|lyric(?:s)?|visualizer|hd|4k|remaster(?:ed)?|feat\.?|ft\.?)"
        r"[^\]\)]*"
        r"[\]\)]",
        re.IGNORECASE,
    )
    cleaned = NOISE_RE.sub("", raw).strip()
    cleaned = re.sub(r"[\[\(]\s*[\]\)]", "", cleaned).strip()

    attempts = []

    # 1. Cleaned query
    attempts.append(f"ytsearch5:{cleaned}")

    # 2. Raw query (if noise brackets were stripped)
    if raw != cleaned:
        attempts.append(f"ytsearch5:{raw}")

    # 3. Bare query — strip all remaining parentheticals
    bare = re.sub(r"[\(\[].*?[\)\]]", "", cleaned).strip()
    if bare and bare != cleaned:
        attempts.append(f"ytsearch5:{bare}")

    # 4. Pipe-segment (drop everything after " | ")
    pipe_segment = re.split(r"\s*\|\s*", bare or cleaned)[0].strip()
    if pipe_segment and pipe_segment != (bare or cleaned):
        attempts.append(f"ytsearch5:{pipe_segment}")

    # 5. First artist only — handles "Artist1, Artist2 - Title" where the
    #    comma-separated list confuses search; retry with just the lead artist.
    comma_match = re.match(r"^([^,]+),\s*[^-]+-\s*(.+)$", cleaned)
    if comma_match:
        first_artist = comma_match.group(1).strip()
        title_part   = comma_match.group(2).strip()
        simplified   = f"{first_artist} - {title_part}"
        attempts.append(f"ytsearch5:{simplified}")

    seen, unique = set(), []
    for a in attempts:
        if a not in seen:
            seen.add(a)
            unique.append(a)
    return unique


async def _apply_loudnorm(filepath: str) -> str:
    normalized = filepath.replace(".mp3", "_norm.mp3")
    try:
        before = os.path.getsize(filepath)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", filepath,
            "-af", "volume=-10dB,loudnorm=I=-24:TP=-2:LRA=11",
            "-codec:a", "libmp3lame",
            normalized,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if os.path.exists(normalized) and os.path.getsize(normalized) > 0:
            after = os.path.getsize(normalized)
            os.replace(normalized, filepath)
            print(f"[loudnorm] applied — before={before/1024:.1f}KB after={after/1024:.1f}KB")
        else:
            print(f"[loudnorm] output missing or empty")
            if stderr:
                print(f"[loudnorm] ffmpeg stderr: {stderr.decode()[-1000:]}")
    except Exception as e:
        print(f"[loudnorm] failed: {e}")
    return filepath