#!/usr/bin/env python3
"""
fixvid.py -- run a single video through video_fix.py and write the result.

video_fix.py is a library of helpers with no dispatcher of its own, so this
script owns the routing from _needs_remux's `fix_type` to the matching repair
function:

    "trim"  -> _trim_to_audio   (stream-copy, no quality loss)
    "audio" -> _remux_fix       (re-encodes video + audio from PCM)
    "video" -> _fix_video_pts   (re-encodes video, copies audio untouched)

Usage:
    ./fixvid.py clip.mp4                      # diagnose + fix -> clip_fixed.mp4
    ./fixvid.py clip.mp4 -o out.mp4           # explicit output path
    ./fixvid.py clip.mp4 -n                   # diagnose only, write nothing
    ./fixvid.py clip.mp4 --force              # repair even if nothing detected
    ./fixvid.py clip.mp4 --normalize          # + loudnorm to -14 LUFS
    ./fixvid.py clip.mp4 --compress 8         # + squeeze to ~8 MB
    ./fixvid.py clip.mp4 --thorough           # scan whole file for open-GOP HEVC,
                                               # not just the first 30s
    ./fixvid.py *.mp4                         # batch; summary at the end

Every run also checks HEVC inputs for open-GOP encoding (the "plays audio
only in Movies & TV / Photos / WMP but fine in VLC/Discord" bug) and
re-encodes to closed-GOP when found -- see video_fix._probe_open_gop_hevc.

Exit codes: 0 = ok (fixed or nothing needed), 1 = a repair failed.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

try:
    import video_fix
except ImportError:
    sys.exit("video_fix.py not found -- put it next to this script "
             "or on PYTHONPATH")


# ── formatting ────────────────────────────────────────────────────────────────

class C:
    """ANSI colours, disabled when not writing to a terminal."""
    on = sys.stdout.isatty()
    dim    = "\033[2m"  if on else ""
    red    = "\033[31m" if on else ""
    green  = "\033[32m" if on else ""
    yellow = "\033[33m" if on else ""
    cyan   = "\033[36m" if on else ""
    bold   = "\033[1m"  if on else ""
    off    = "\033[0m"  if on else ""


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


FIX_LABELS = {
    "trim":  "inflated container duration (stream-copy trim, lossless)",
    "audio": "broken audio timing (re-encode from PCM)",
    "video": "stretched video PTS (re-encode video, keep audio)",
}

OPEN_GOP_LABEL = "open-GOP HEVC (re-encode to closed-GOP so Windows Media Foundation can play it)"


# ── probing ───────────────────────────────────────────────────────────────────

def inspect(path):
    """Container/stream durations plus a bitrate sanity figure."""
    out = subprocess.check_output([
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", path,
    ])
    d = json.loads(out)
    info = {
        "container": float(d["format"].get("duration", 0) or 0),
        "size": int(d["format"].get("size", 0) or 0),
        "container_bitrate": int(d["format"].get("bit_rate", 0) or 0),
    }
    for s in d.get("streams", []):
        t = s.get("codec_type")
        if t in ("video", "audio") and t not in info:
            info[t] = float(s.get("duration", 0) or 0)
            info[f"{t}_codec"] = s.get("codec_name", "?")
            info[f"{t}_bitrate"] = int(s.get("bit_rate", 0) or 0)
    return info


def show(label, info):
    print(f"  {C.dim}{label:<9}{C.off}"
          f"video={info.get('video', 0):8.3f}s  "
          f"audio={info.get('audio', 0):8.3f}s  "
          f"container={info.get('container', 0):8.3f}s  "
          f"{human(info.get('size', 0))}")
    vb, cb = info.get("video_bitrate", 0), info.get("container_bitrate", 0)
    if vb and cb and vb > cb * 2:
        print(f"  {C.dim}{'':<9}{C.yellow}! video stream is {vb/1e6:.2f} Mbps but "
              f"container reports {cb/1e6:.2f} Mbps overall{C.off}")


# ── the actual repair ─────────────────────────────────────────────────────────

def repair(src, dest, fix_type, value):
    """Route to the right video_fix helper. Returns True on success."""
    if fix_type == "trim":
        return video_fix._trim_to_audio(src, dest, value)
    if fix_type == "video":
        return video_fix._fix_video_pts(src, dest, value)
    return video_fix._remux_fix(src, dest, value)


def process(path, args):
    print(f"\n{C.bold}{os.path.basename(path)}{C.off}")

    if not os.path.exists(path):
        print(f"  {C.red}no such file{C.off}")
        return False

    before = inspect(path)
    show("before", before)

    if video_fix._is_corrupt(path):
        print(f"  {C.yellow}note{C.off}     video stream emits decode errors")

    needs, value, fix_type = video_fix._needs_remux(path)

    if needs:
        print(f"  {C.cyan}detected{C.off} {FIX_LABELS.get(fix_type, fix_type)}")
        print(f"  {C.dim}         fix_type={fix_type!r}  value={value:.6f}{C.off}")
    else:
        print(f"  {C.green}clean{C.off}    no timing problems detected")

    open_gop, open_gop_verdict = video_fix._probe_open_gop_hevc(path, thorough=args.thorough)
    if open_gop:
        print(f"  {C.cyan}detected{C.off} {OPEN_GOP_LABEL}")
        print(f"  {C.dim}         {open_gop_verdict}{C.off}")

    extras = args.normalize or args.compress
    if not needs and not open_gop and not args.force and not extras:
        return True

    if args.dry_run:
        print(f"  {C.dim}dry run -- nothing written{C.off}")
        return True

    dest = args.output or f"{os.path.splitext(path)[0]}_fixed.mp4"
    if os.path.abspath(dest) == os.path.abspath(path):
        print(f"  {C.red}refusing to overwrite the input{C.off}")
        return False

    # Chain stages through temp files so a later failure never eats the input.
    tmpdir = tempfile.mkdtemp()
    try:
        current = path
        stages = []
        if needs or args.force:
            stages.append(("repair", lambda s, d: repair(
                s, d, fix_type if needs else "audio", value if needs else 1.0)))
        if open_gop:
            stages.append(("open-gop fix", lambda s, d: video_fix._fix_open_gop_hevc(s, d)))
        if args.normalize:
            stages.append(("normalize", video_fix._normalize_audio))
        if args.compress:
            def _sq(s, d):
                dur, h = video_fix._probe(s)
                if dur <= 0:
                    return False
                return video_fix._compress_to_target(s, d, args.compress, dur, h)
            stages.append(("compress", _sq))

        for i, (name, fn) in enumerate(stages):
            out = os.path.join(tmpdir, f"stage{i}.mp4")
            print(f"  {C.dim}running {name}...{C.off}", flush=True)
            if not fn(current, out):
                print(f"  {C.red}{name} failed{C.off}")
                return False
            current = out

        shutil.copy2(current, dest)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    after = inspect(dest)
    show("after", after)

    # Confirm the repair actually took, rather than trusting the exit code.
    still, _, _ = video_fix._needs_remux(dest)
    if still:
        print(f"  {C.yellow}warning{C.off}  output is still flagged")
    if open_gop:
        still_open_gop, _ = video_fix._probe_open_gop_hevc(dest)
        if still_open_gop:
            print(f"  {C.yellow}warning{C.off}  output is still open-GOP HEVC")
    print(f"  {C.green}wrote{C.off}    {dest}")
    return True


def main():
    ap = argparse.ArgumentParser(
        description="Diagnose and repair a video using video_fix.py")
    ap.add_argument("files", nargs="+", help="input video(s)")
    ap.add_argument("-o", "--output",
                    help="output path (single input only; default: <name>_fixed.mp4)")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="diagnose only, write nothing")
    ap.add_argument("--force", action="store_true",
                    help="run the repair even when nothing is detected")
    ap.add_argument("--normalize", action="store_true",
                    help="also loudness-normalise to -14 LUFS")
    ap.add_argument("--compress", type=float, metavar="MB",
                    help="also compress to roughly this size in MB")
    ap.add_argument("--thorough", action="store_true",
                    help="scan the whole file (not just the first 30s) for open-GOP HEVC")
    args = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"{tool} not found on PATH")

    if args.output and len(args.files) > 1:
        sys.exit("-o only works with a single input file")

    results = [process(f, args) for f in args.files]

    if len(results) > 1:
        ok = sum(results)
        print(f"\n{ok}/{len(results)} succeeded")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
