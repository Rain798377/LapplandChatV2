import os
import re
import glob
import shutil
import asyncio
import secrets
import tempfile
import aiohttp
import yt_dlp
import discord
from discord import app_commands
from config import MAX_FILE_SIZE_MB, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, FILE_SERVER_PATH, FILE_SERVER_BASE_URL, FILE_EXPIRY_SECONDS



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


# ── Download command helpers ───────────────────────────────────────────────────

def _probe(filepath: str) -> tuple[float, int]:
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


def _needs_remux(filepath: str) -> tuple[bool, float, str]:
    """
    Detect whether a file has timing problems that require a remux.

    Returns (needs_fix: bool, pts_mul: float, fix_type: str) where:
      - pts_mul  : video PTS multiplier to apply during the fix
      - fix_type : "audio"  → broken audio; use _remux_fix (re-encodes audio from PCM)
                   "video"  → broken video PTS; use _fix_video_pts (re-encodes video,
                              copies audio untouched)

    Detection covers five independent failure modes, checked in priority order:

    ── CASE 1: Explicit HE-AAC (existing behaviour, unchanged) ──────────────
    Profile string contains "he" (e.g. "HE-AAC v1", "HE-AAC v2").
    The SBR layer doubles the effective sample rate, so the muxed audio
    timestamps run at 2× real-time.  Fix: slow video by 0.5× (audio fix path).

    ── CASE 2: Implicit SBR / LC-labelled HE-AAC ────────────────────────────
    Some encoders (iOS screen recorder, libfdk_aac in certain modes, many
    Android/OEM encoders) write profile=LC in the MPEG-4 Audio Object Type
    (AOT 2) even though SBR is active.  ffprobe reports "LC" because it reads
    the AOT, not the decoded frame behaviour.

    Signature: profile == "LC" but average audio samples/frame is in
    [1200, 2200] — well above the standard AAC-LC window of 1024 but below
    the HE-AAC v2 ceiling of ~2048.  The old threshold of >3000 completely
    missed this range.

    Guard: only act when the stream durations are actually misaligned
    (audio_dur / video_dur > 1.4 or < 0.71).  A well-muxed file can have
    ~2048 spf (SBR present but correctly timestamped) while durations match
    perfectly — that is NOT broken and must not be touched.

    Fix: re-encode audio from decoded PCM (audio fix path); pts_mul =
    audio_dur / video_dur so any residual drift is absorbed.

    ── CASE 3: Edit-list / encoder-delay mismatch ───────────────────────────
    The MP4 edit list (elst box) trims the video start by T_v seconds, while
    the audio track has an encoder pre-roll of T_a seconds (negative start
    PTS).  When T_a ≠ T_v the streams are misaligned by (T_a − T_v).

    Fix: audio fix path, pts_mul = 1.0 (re-encode normalises timestamps).

    ── CASE 4: Variable audio frame duration ────────────────────────────────
    A well-formed AAC-LC stream has exactly 1024 samples per frame.
    High coefficient of variation (std_dev / mean > 0.15) signals broken
    muxer timing.

    Fix: audio fix path, pts_mul computed from actual durations.

    ── CASE 5: Video / audio duration mismatch (video PTS wrong) ────────────
    Occurs when yt-dlp merges streams whose PTS were never aligned — most
    commonly a DASH video track stamped with doubled timestamps (e.g. the
    source was 30 fps but the container claims 60 fps with the original 30-fps
    PTS values, making every frame appear twice as long).  Symptom: video plays
    at half speed while audio is perfectly normal.

    Signature: video_dur / audio_dur > 1.4   (video is at least 40% longer
               than audio — way outside any normal encoder-delay margin).

    Fix: video fix path — re-encode video with setpts=(audio_dur/video_dur)*PTS
    so timestamps are squished to match audio exactly.  Audio is stream-copied
    untouched.  pts_mul = audio_dur / video_dur (e.g. 0.5 for 2× slow video).
    """
    import subprocess, json, statistics

    try:
        # ── Full stream + packet metadata ────────────────────────────────────
        stream_out = subprocess.check_output([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", "-show_format", filepath,
        ], timeout=30)
        stream_data = json.loads(stream_out)
        streams = stream_data.get("streams", [])

        audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
        video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)

        if not audio_stream:
            return False, 1.0, "audio"

        # ── Stream duration helper ────────────────────────────────────────────
        def _stream_duration_s(s: dict) -> float:
            d = s.get("duration")
            if d and d != "N/A":
                return float(d)
            ts = s.get("duration_ts")
            tb = s.get("time_base", "1/1")
            if ts and tb:
                num, den = (int(x) for x in tb.split("/"))
                return int(ts) * num / den
            return 0.0

        audio_dur_s = _stream_duration_s(audio_stream)
        video_dur_s = _stream_duration_s(video_stream) if video_stream else 0.0

        # ── CASE 5: Video/audio duration mismatch ────────────────────────────
        # Check this FIRST — it is a video-side bug and must not be confused
        # with the audio-side bugs in Cases 1-4.  If the video track is more
        # than 40% longer than the audio track, the video PTS are wrong.
        # The fix is purely on the video side (audio stream-copied as-is).
        # A 40% threshold safely clears normal encoder-delay margins (which are
        # never more than ~100ms) while catching 2×, 1.5× and other multiples.
        if audio_dur_s > 1.0 and video_dur_s > 1.0:
            ratio = video_dur_s / audio_dur_s
            if ratio > 1.4:
                pts_mul = audio_dur_s / video_dur_s  # e.g. 0.5 for 2× slow video
                return True, pts_mul, "video"

        # ── Raw audio packet durations ────────────────────────────────────────
        pkt_out = subprocess.check_output([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_packets", "-select_streams", "a:0", filepath,
        ], timeout=60)
        pkt_data  = json.loads(pkt_out)
        packets   = pkt_data.get("packets", [])
        durations = [int(p["duration"]) for p in packets if p.get("duration") not in (None, "N/A")]

        if not durations:
            return False, 1.0, "audio"

        profile         = audio_stream.get("profile", "").lower()
        sample_rate     = int(audio_stream.get("sample_rate", 44100))
        audio_start_pts = int(audio_stream.get("start_pts", 0))

        # Prefer measured packet data over metadata claims
        total_ts_measured  = sum(durations)
        nb_frames_measured = len(durations)
        samples_per_frame  = total_ts_measured / nb_frames_measured

        # ── CASE 1: Explicit HE-AAC (unchanged from original) ────────────────
        if "he" in profile:
            if samples_per_frame > 3000:
                return True, 0.5, "audio"
            # Explicit HE-AAC but frame size not wildly doubled — fall through.

        # ── CASE 2: Implicit SBR / LC-labelled HE-AAC ────────────────────────
        # Standard AAC-LC = 1024 samples/frame.
        # HE-AAC (with or without explicit profile label) produces ~2048.
        # Threshold 1200 safely separates 1024 (good) from 2048 (bad),
        # catching slightly-off values like 2042 that the old >3000 missed.
        #
        # Guard: a well-muxed file can legitimately have ~2048 spf while its
        # stream durations are perfectly matched (SBR present but correctly
        # timestamped).  Only act when streams are actually misaligned.
        if 1200 < samples_per_frame <= 2200:
            if audio_dur_s > 1.0 and video_dur_s > 1.0:
                ratio = audio_dur_s / video_dur_s
                if ratio > 1.4 or ratio < 0.71:
                    pts_mul = ratio  # audio_dur / video_dur absorbs the drift
                    return True, pts_mul, "audio"
                # else: durations are aligned — SBR present but mux is fine
            else:
                # No reliable duration info — assume broken, use safe fallback
                return True, 0.5, "audio"

        # ── CASE 2b: Inflated duration_ts on genuine AAC-LC ──────────────────
        # Some muxers (CapCut and similar) write corrupt per-frame duration_ts
        # values in the audio sample table even though the bitstream is genuine
        # AAC-LC (1024 samples/frame, no SBR).  The inflation factor is not
        # necessarily exactly 2× — this file uses 1965 instead of 1024 (≈1.92×).
        #
        # The container-level audio duration looks plausible because it is
        # derived from the same inflated duration_ts values, so the audio/video
        # ratio appears normal (~1.0).  The only way to catch this is to compute
        # the REAL audio duration from the frame count:
        #
        #   real_audio_dur = nb_frames * 1024 / sample_rate
        #
        # If real_audio_dur is significantly shorter than video_dur the
        # timestamps are lying.  The _remux_fix path decodes to PCM (ignoring
        # the broken timestamps entirely) so pts_mul = real_audio_dur / video_dur
        # corrects the video speed to match the true audio length.
        #
        # Only runs when profile is "lc" or unknown — explicit HE-AAC is
        # already handled by Case 1, and Case 2 above covers implicit SBR.
        if "he" not in profile and video_dur_s > 1.0 and nb_frames_measured >= 10:
            real_audio_dur_s = nb_frames_measured * 1024 / sample_rate
            real_ratio = real_audio_dur_s / video_dur_s
            if real_ratio < 0.71:
                pts_mul = real_ratio  # video slowed to match true audio length
                return True, pts_mul, "audio"

        # ── CASE 3: Edit-list / encoder-delay mismatch ───────────────────────
        encoder_delay_threshold_pts = sample_rate * 0.010  # 10 ms
        if audio_start_pts < -encoder_delay_threshold_pts:
            trace_out = subprocess.run(
                ["ffprobe", "-v", "trace", filepath],
                capture_output=True, text=True, timeout=15,
            )
            trace = trace_out.stderr + trace_out.stdout
            has_edit_list = "type:'elst'" in trace
            if has_edit_list:
                import re as _re
                match = _re.search(r"media time:\s*([\d.]+)", trace)
                if match:
                    video_tb_den = int(video_stream.get("time_base", "1/15360").split("/")[1]) if video_stream else 15360
                    video_edit_offset_s = float(match.group(1)) / video_tb_den
                    audio_preroll_s = abs(audio_start_pts) / sample_rate
                    drift_s = abs(audio_preroll_s - video_edit_offset_s)
                    if drift_s > 0.010:
                        return True, 1.0, "audio"
                else:
                    return True, 1.0, "audio"

        # ── CASE 4: Variable audio frame duration ────────────────────────────
        if len(durations) >= 10:
            mean_dur  = statistics.mean(durations)
            stdev_dur = statistics.stdev(durations)
            cv        = stdev_dur / mean_dur if mean_dur else 0.0
            if cv > 0.15:
                pts_mul = (audio_dur_s / video_dur_s) if (audio_dur_s > 0 and video_dur_s > 0) else 1.0
                return True, pts_mul, "audio"

        return False, 1.0, "audio"

    except Exception:
        return False, 1.0, "audio"


def _remux_fix(src: str, dest: str, pts_mul: float = 1.0) -> bool:
    import subprocess, os
    wav = src + "_audio_raw.wav"
    try:
        r1 = subprocess.run([
            "ffmpeg", "-y",
            "-i", src,
            "-vn", "-c:a", "pcm_s16le",
            "-ar", "22050",
            "-ac", "2",
            wav,
        ], capture_output=True, timeout=120)
        if r1.returncode != 0 or not os.path.exists(wav):
            return False

        r2 = subprocess.run([
            "ffmpeg", "-y",
            "-i", src,
            "-i", wav,
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


def _fix_video_pts(src: str, dest: str, pts_mul: float) -> bool:
    """
    Fix broken video PTS by re-encoding the video stream with a setpts multiplier
    while stream-copying the audio exactly as-is.

    Use this when the video is the broken side (e.g. video plays 2× too slow
    because PTS were stamped at double the correct rate).  Unlike _remux_fix,
    this never touches the audio — it is already correct and should not be
    resampled or re-encoded.

    pts_mul examples:
      0.5  → video was 2× too slow; squish PTS in half → plays at correct speed
      0.75 → video was 1.33× too slow; etc.
    """
    import subprocess, os
    try:
        result = subprocess.run([
            "ffmpeg", "-y",
            "-i", src,
            "-map", "0:v", "-map", "0:a",
            "-vf", f"setpts={pts_mul:.10g}*PTS",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-video_track_timescale", "90000",
            "-c:a", "copy",          # audio is correct — do not touch it
            "-movflags", "+faststart",
            dest,
        ], capture_output=True, timeout=300)
        return result.returncode == 0 and os.path.exists(dest)
    except Exception:
        return False


def _compress_to_target(src: str, dest: str, target_mb: float, duration_s: float, src_height: int) -> bool:
    import subprocess

    headroom    = 0.90
    target_bits = target_mb * 1024 * 1024 * 8 * headroom
    audio_bps   = 96_000
    video_bps   = max(100_000, int(target_bits / duration_s) - audio_bps)

    if src_height > 480 and video_bps < 500_000:
        scale = "scale=-2:480"
    elif src_height > 720 and video_bps < 1_000_000:
        scale = "scale=-2:720"
    else:
        scale = "scale=trunc(iw/2)*2:trunc(ih/2)*2"

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


def copy_to_file_server(src: str, preferred_name: str = "") -> str | None:
    """
    Copy `src` to the file server's watched folder.
    If `preferred_name` is provided, uses it as the filename (sanitized);
    otherwise falls back to a random 256-bit hex token.
    Remuxes with faststart so browsers can stream it.
    Returns a direct download URL, or None on failure.
    Schedules auto-deletion after FILE_EXPIRY_SECONDS.
    """
    import subprocess
    try:
        os.makedirs(FILE_SERVER_PATH, exist_ok=True)
        ext = os.path.splitext(src)[1]  # preserve .mp4 etc.

        if preferred_name:
            # Sanitize: strip path separators and other dangerous chars
            safe = re.sub(r'[\\/:*?"<>|]', "_", preferred_name).strip(" .")
            hashed_name = f"{safe}{ext}" if safe else secrets.token_hex(32) + ext
        else:
            hashed_name = secrets.token_hex(32) + ext  # 64-char random hex (256-bit)

        dest = os.path.join(FILE_SERVER_PATH, hashed_name)
        # Remux with faststart so browsers can stream without downloading the whole file
        result = subprocess.run([
            "ffmpeg", "-y", "-i", src,
            "-c", "copy",
            "-movflags", "+faststart",
            dest,
        ], capture_output=True, timeout=120)
        if result.returncode != 0 or not os.path.exists(dest):
            # Fall back to plain copy if remux fails
            shutil.copy2(src, dest)
        # Schedule deletion after expiry
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


async def attempt_download(
    url: str,
    height: int,
    status_msg=None,
    clean_filename: str = "",
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

        size_mb = os.path.getsize(src) / (1024 * 1024)

        # ── Under limit — send directly ───────────────────────────────────────
        if size_mb <= MAX_FILE_SIZE_MB:
            dest = os.path.join(tempfile.gettempdir(), base_name)
            shutil.copy2(src, dest)
            return dest, None

        # ── Over limit — compress for Discord, send original to file server ───
        await _status(f"File is {size_mb:.1f} MB — compressing video for Discord")
        duration, src_height = await loop.run_in_executor(None, lambda: _probe(src))

        # Copy original to file server first (while still in tmpdir)
        file_server_url: str | None = None
        raw_dest = os.path.join(tempfile.gettempdir(), base_name)
        shutil.copy2(src, raw_dest)

    # tmpdir gone — work with raw_dest from here
    # Copy to file server first so we have the original safe
    file_server_url = copy_to_file_server(raw_dest, preferred_name=clean_filename)

    # Now compress from raw_dest for Discord
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
                # Compressed for Discord + original on file server
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

    # Compression failed/impossible — file server only, nothing for Discord
    return None, file_server_url


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
        filepath   = None
        fs_url     = None
        status_msg = await interaction.followup.send("Starting download…", wait=True)
        try:
            target_height = 1080 if quality == "auto" else int(quality)
            filepath, fs_url = await attempt_download(url, target_height, status_msg, clean_filename)

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
                    # Over 25MB: send compressed to Discord + original link
                    await interaction.followup.send(
                        file=discord.File(filepath, display_name),
                        content=f"-# Compressed for Discord. Original quality: <{fs_url}>"
                    )
                else:
                    # Under 25MB: send normally
                    await interaction.followup.send(file=discord.File(filepath, display_name))

                asyncio.create_task(delayed_delete(status_msg, delay=1))

            elif fs_url:
                # Compression failed entirely — file server only
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