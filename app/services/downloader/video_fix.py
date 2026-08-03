import os
import json
import statistics
import subprocess


def _probe(filepath: str) -> tuple[float, int]:
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


def _count_frames(filepath: str, timeout: int = 120) -> int | None:
    """
    Decode-based frame count — ground truth for the video stream.

    Stream-level `nb_frames` (from `-show_streams`) is just a metadata field
    written by the muxer, and on some broken encoder writes it gets inflated
    by the *same* bad factor as `duration_ts` rather than being an
    independently-measured count. When that happens, comparing `nb_frames /
    fps` against anything else just compares the bad duration against
    itself. Decoding and counting actual frames (`-count_frames`) sidesteps
    that since it never reads `duration_ts` or `nb_frames` at all.

    Slower than reading metadata, so this is only called when the metadata
    value is already suspect — not on every file.
    """
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "error", "-count_frames",
            "-select_streams", "v:0",
            "-show_entries", "stream=nb_read_frames",
            "-print_format", "json",
            filepath,
        ], timeout=timeout)
        info = json.loads(out)
        streams = info.get("streams", [])
        if streams:
            n = streams[0].get("nb_read_frames")
            if n and n != "N/A":
                return int(n)
    except Exception:
        pass
    return None


def _needs_remux(filepath: str) -> tuple[bool, float, str]:
    """
    Detect whether a file has timing problems that require a remux.

    Returns (needs_fix: bool, pts_mul: float, fix_type: str) where:
      - pts_mul  : video PTS multiplier (for "video" fix_type) or audio ratio
                   (for "audio" fix_type); unused for "trim" fix_type
      - fix_type : "audio"  → broken audio; use _remux_fix (re-encodes audio from PCM)
                   "video"  → broken video PTS; use _fix_video_pts (re-encodes video,
                              copies audio untouched)
                   "trim"   → video duration metadata inflated but frames are correct;
                              use _trim_to_audio (stream-copy, -to audio_dur)

    Detection covers six independent failure modes, checked in priority order.
    All duration-ratio comparisons use a dynamic threshold derived from the
    actual stream durations rather than a fixed magic number: anything beyond
    a 10% relative difference (DURATION_SKEW_TOLERANCE = 0.10) is considered
    misaligned.

    ── CASE 1: Explicit HE-AAC ──────────────────────────────────────────────
    Profile string contains "he" (e.g. "HE-AAC v1", "HE-AAC v2").
    The SBR layer doubles the effective sample rate, so the muxed audio
    timestamps run at 2× real-time.  Fix: slow video by 0.5× (audio fix path).

    ── CASE 2: Implicit SBR / LC-labelled HE-AAC ────────────────────────────
    Some encoders write profile=LC even though SBR is active. ffprobe reports
    "LC" because it reads the AOT, not the decoded frame behaviour.
    Signature: profile == "LC" but average audio samples/frame is in
    [1200, 2200].  Guard: only act when stream durations are actually
    misaligned beyond DURATION_SKEW_TOLERANCE.
    Fix: re-encode audio from decoded PCM; pts_mul = audio_dur / video_dur.

    ── CASE 2b: Inflated duration_ts on genuine AAC-LC ──────────────────────
    Real audio duration (nb_frames * 1024 / sample_rate) is significantly
    shorter than video_dur_s, indicating the duration_ts field is wrong.
    Fix: audio fix path, pts_mul = real_audio_dur / video_dur.

    ── CASE 3: Edit-list / encoder-delay mismatch ───────────────────────────
    The MP4 edit list (elst box) trims the video start by T_v seconds, while
    the audio track has an encoder pre-roll of T_a seconds (negative start
    PTS).  When T_a ≠ T_v the streams are misaligned.
    Fix: audio fix path, pts_mul = 1.0 (re-encode normalises timestamps).

    ── CASE 4: Variable audio frame duration ────────────────────────────────
    A well-formed AAC-LC stream has exactly 1024 samples per frame.
    High coefficient of variation (std_dev / mean > 0.15) signals broken
    muxer timing.
    Fix: audio fix path, pts_mul computed from actual durations.

    ── CASE 5: Video PTS stretched (video plays too slow) ───────────────────
    video_dur_s is significantly longer than audio_dur_s AND the frame count
    at the declared fps does not match audio_dur_s — meaning the PTS values
    themselves are wrong (e.g. DASH 30fps track muxed with 60fps timestamps).
    Fix: video fix path — re-encode video with setpts=pts_mul*PTS.
    pts_mul = audio_dur_s / video_dur_s.

    ── CASE 6: Inflated container duration (frames are correct) ─────────────
    video_dur_s is significantly longer than audio_dur_s BUT
    nb_frames / avg_fps ≈ audio_dur_s — the actual encoded frames cover only
    the audio-length window; the container duration field is simply wrong.
    Symptom: video plays fine for audio_dur_s then jumps/freezes to the end.
    Fix: trim — stream-copy with -to audio_dur_s, no re-encode needed.
    pts_mul is returned as audio_dur_s (the trim target) for the caller.

    NOTE: stream-metadata nb_frames can itself be inflated by the same bad
    factor as duration_ts on certain broken muxer writes (it wasn't measured
    independently — it was derived from the corrupt duration field). When
    nb_frames/fps merely echoes video_dur_s instead of giving new
    information, this falls back to an actual decoded frame count
    (_count_frames) before deciding between CASE 5 and CASE 6.
    """
    # Fraction beyond which two durations are considered misaligned.
    # e.g. 0.10 → flag if one stream is >10% longer than the other.
    DURATION_SKEW_TOLERANCE = 0.10

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

        # ── CASE 5 / 6: Video longer than audio ──────────────────────────────
        if audio_dur_s > 1.0 and video_dur_s > 1.0:
            ratio = video_dur_s / audio_dur_s          # >1 means video is longer
            skew  = ratio - 1.0                        # normalised excess

            if skew > DURATION_SKEW_TOLERANCE:
                # Determine whether the actual encoded frames match audio_dur_s
                # (CASE 6 / trim) or whether the PTS themselves are stretched
                # (CASE 5 / rescale).
                #
                # Heuristic: compute the frame-count-implied duration using the
                # avg_frame_rate reported by ffprobe.  If that implied duration
                # is within tolerance of audio_dur_s, the frames are fine and
                # only the container metadata is wrong → trim.
                nb_frames  = int(video_stream.get("nb_frames", 0) or 0)
                fps_str    = video_stream.get("avg_frame_rate", "0/1")
                try:
                    fn, fd    = (int(x) for x in fps_str.split("/"))
                    fps       = fn / fd if fd else 0.0
                except Exception:
                    fps = 0.0

                implied_dur = (nb_frames / fps) if (nb_frames > 0 and fps > 0) else None

                # Guard: on some broken muxer writes, stream-metadata
                # nb_frames is inflated by the same bad factor as
                # duration_ts (it wasn't independently measured — it was
                # derived from the same corrupt duration field). When that
                # happens, implied_dur just echoes video_dur_s back at us
                # instead of giving an independent signal, so the check
                # below would always fail and we'd wrongly fall through to
                # CASE 5. Detect that and re-derive implied_dur from an
                # actual decode-based frame count instead.
                if implied_dur is not None and fps > 0:
                    implied_vs_video_skew = abs(implied_dur - video_dur_s) / video_dur_s
                    if implied_vs_video_skew <= DURATION_SKEW_TOLERANCE:
                        real_nb_frames = _count_frames(filepath)
                        if real_nb_frames:
                            implied_dur = real_nb_frames / fps

                if implied_dur is not None:
                    implied_skew = abs(implied_dur - audio_dur_s) / audio_dur_s
                    if implied_skew <= DURATION_SKEW_TOLERANCE:
                        # CASE 6: frames cover exactly audio_dur_s — just trim
                        return True, audio_dur_s, "trim"

                # CASE 5: PTS are genuinely stretched — re-encode with setpts
                pts_mul = audio_dur_s / video_dur_s
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

        total_ts_measured  = sum(durations)
        nb_frames_measured = len(durations)
        samples_per_frame  = total_ts_measured / nb_frames_measured

        # ── CASE 1: Explicit HE-AAC ───────────────────────────────────────────
        if "he" in profile:
            if samples_per_frame > 3000:
                return True, 0.5, "audio"

        # ── CASE 2: Implicit SBR / LC-labelled HE-AAC ────────────────────────
        if 1200 < samples_per_frame <= 2200:
            if audio_dur_s > 1.0 and video_dur_s > 1.0:
                ratio = audio_dur_s / video_dur_s
                skew  = abs(ratio - 1.0)
                if skew > DURATION_SKEW_TOLERANCE:
                    pts_mul = ratio
                    return True, pts_mul, "audio"
            else:
                return True, 0.5, "audio"

        # ── CASE 2b: Inflated duration_ts on genuine AAC-LC ──────────────────
        if "he" not in profile and video_dur_s > 1.0 and nb_frames_measured >= 10:
            real_audio_dur_s = nb_frames_measured * 1024 / sample_rate
            real_ratio = real_audio_dur_s / video_dur_s
            shortfall  = 1.0 - real_ratio               # how much shorter real audio is
            if shortfall > DURATION_SKEW_TOLERANCE:
                pts_mul = real_ratio
                return True, pts_mul, "audio"

        # ── CASE 3: Edit-list / encoder-delay mismatch ───────────────────────
        encoder_delay_threshold_pts = sample_rate * 0.010
        if audio_start_pts < -encoder_delay_threshold_pts:
            trace_out = subprocess.run(
                ["ffprobe", "-v", "trace", filepath],
                capture_output=True, timeout=15,
            )
            trace = trace_out.stderr.decode("utf-8", errors="replace") + trace_out.stdout.decode("utf-8", errors="replace")
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
    try:
        result = subprocess.run([
            "ffmpeg", "-y",
            "-i", src,
            "-map", "0:v", "-map", "0:a",
            "-vf", f"setpts={pts_mul:.10g}*PTS",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-video_track_timescale", "90000",
            "-c:a", "copy",
            "-movflags", "+faststart",
            dest,
        ], capture_output=True, timeout=300)
        return result.returncode == 0 and os.path.exists(dest)
    except Exception:
        return False
    
def _trim_to_audio(src: str, dest: str, audio_dur_s: float) -> bool:
    """
    Fix a file whose video container duration is inflated but whose encoded
    frames are correct (CASE 6).  Stream-copies both streams with -to
    audio_dur_s so no re-encode is needed.

    audio_dur_s is passed in directly from _needs_remux (pts_mul field when
    fix_type == "trim") so there is no second ffprobe call here.
    """
    try:
        result = subprocess.run([
            "ffmpeg", "-y",
            "-i", src,
            "-to", f"{audio_dur_s:.3f}",
            "-c", "copy",
            "-movflags", "+faststart",
            dest,
        ], capture_output=True, timeout=300)
        return result.returncode == 0 and os.path.exists(dest)
    except Exception:
        return False


def _is_corrupt(filepath: str) -> bool:
    """
    Performs a deep integrity check by attempting to decode the video stream.
    Returns True if decoding fails (file is corrupt), False if healthy.

    Checks both return code AND stderr output: severely corrupt files can
    exit 0 while still emitting error lines that indicate bad NAL units,
    invalid bitstream, or unreadable headers.
    """
    try:
        result = subprocess.run([
            "ffmpeg", "-v", "error", "-i", filepath,
            "-map", "0:v:0", "-f", "null", "-"
        ], capture_output=True, timeout=60)

        if result.returncode != 0:
            return True

        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        return bool(stderr)
    except Exception:
        return True

def _normalize_audio(src: str, dest: str) -> bool:
    """
    Two-pass EBU R128 loudnorm to -14 LUFS.

    Branches on whether the source has a video stream:
      - Audio-only (MP3, etc.): decoded -> loudnorm -> re-encoded MP3, same
        container/extension as src. No size bloat from re-containerizing.
      - Video: audio loudnorm + -c:v copy, output is MP4.

    Returns True on success. On failure dest is not written.
    """
    # -- Detect whether source has a video stream -----------------------------
    try:
        probe = subprocess.check_output([
            "ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", src,
        ], timeout=30)
        streams = json.loads(probe).get("streams", [])
        has_video = any(s.get("codec_type") == "video" for s in streams)
    except Exception:
        has_video = False

    # -- Pass 1: measure ------------------------------------------------------
    try:
        r1 = subprocess.run([
            "ffmpeg", "-y", "-i", src,
            "-af", "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json",
            "-f", "null", "-",
        ], capture_output=True, timeout=300)

        stderr = r1.stderr.decode("utf-8", errors="replace")
        start  = stderr.rfind("{")
        end    = stderr.rfind("}") + 1
        if start == -1 or end == 0:
            return False
        stats = json.loads(stderr[start:end])
    except Exception:
        return False

    # -- Pass 2: apply --------------------------------------------------------
    af = (
        f"loudnorm=I=-14:TP=-1.5:LRA=11"
        f":measured_I={stats['input_i']}"
        f":measured_TP={stats['input_tp']}"
        f":measured_LRA={stats['input_lra']}"
        f":measured_thresh={stats['input_thresh']}"
        f":offset={stats['target_offset']}"
        f":linear=true:print_format=summary"
    )

    try:
        if has_video:
            cmd = [
                "ffmpeg", "-y", "-i", src,
                "-af", af,
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                "-movflags", "+faststart",
                dest,
            ]
        else:
            # Audio-only: decode -> loudnorm -> MP3, no container change
            src_ext = os.path.splitext(src)[1].lower()
            codec   = "libmp3lame" if src_ext == ".mp3" else "aac"
            bitrate = "320k"       if src_ext == ".mp3" else "192k"
            cmd = [
                "ffmpeg", "-y", "-i", src,
                "-af", af,
                "-c:a", codec, "-b:a", bitrate, "-ar", "44100", "-ac", "2",
                "-vn",
                dest,
            ]
        r2 = subprocess.run(cmd, capture_output=True, timeout=300)
        return r2.returncode == 0 and os.path.exists(dest)
    except Exception:
        return False



def _compress_to_target(src: str, dest: str, target_mb: float, duration_s: float, src_height: int) -> bool:
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