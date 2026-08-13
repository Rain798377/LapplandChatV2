"""
gpu_worker.py — client for the laptop-side NVENC encode worker (see
gpu_worker/README.md at the repo root for the server side).

Callers hand remote_encode() an ffmpeg argument list plus local input files;
it uploads them to the laptop, which runs the same job with `h264_nvenc`
instead of `libx264` and streams the result back. This module never raises —
any failure (unset config, laptop off/unreachable, ffmpeg error on the
worker) just returns None so the caller falls back to its existing local CPU
encode unchanged.

video_fix.py imports this lazily (try/except ImportError) since it also runs
standalone via fixvid.py outside the bot's package -- when `core` isn't on
the path, remote_encode is simply unavailable and everything runs on CPU
exactly as it did before this module existed.
"""

import json
import time

import requests

from core.colors import *
from core.config import (
    GPU_WORKER_URL,
    GPU_WORKER_API_KEY,
    GPU_WORKER_CONNECT_TIMEOUT,
    GPU_WORKER_RETRY_COOLDOWN,
)

_unavailable_until = 0.0


def remote_encode(args: list[str], inputs: list[str], output_suffix: str = ".mp4", timeout: float = 300) -> bytes | None:
    """
    Run one ffmpeg job on the laptop's GPU instead of locally.

    `args` is the ffmpeg argument list (everything after "ffmpeg -y"), using
    the literal placeholder tokens "{input0}", "{input1}", ... matching the
    order of `inputs`, and "{output}" for the result path -- the worker
    substitutes these with its own local temp paths before running ffmpeg.

    Returns the encoded bytes on success, or None on any failure.
    """
    global _unavailable_until

    if not GPU_WORKER_URL or not GPU_WORKER_API_KEY:
        return None
    if time.time() < _unavailable_until:
        return None

    files = []
    try:
        files = [("inputs", (f"input{i}", open(path, "rb"))) for i, path in enumerate(inputs)]
        response = requests.post(
            f"{GPU_WORKER_URL.rstrip('/')}/encode",
            data={"args": json.dumps(args), "output_suffix": output_suffix},
            files=files,
            headers={"X-Api-Key": GPU_WORKER_API_KEY},
            timeout=(GPU_WORKER_CONNECT_TIMEOUT, timeout),
        )
    except requests.exceptions.RequestException as e:
        _unavailable_until = time.time() + GPU_WORKER_RETRY_COOLDOWN
        print(f"{YELLOW}[gpu_worker] laptop unreachable ({e.__class__.__name__}), "
              f"falling back to CPU for {GPU_WORKER_RETRY_COOLDOWN}s{RESET}", flush=True)
        return None
    finally:
        for _, (_, fh) in files:
            fh.close()

    if response.status_code != 200:
        print(f"{YELLOW}[gpu_worker] remote encode failed ({response.status_code}), falling back to CPU{RESET}", flush=True)
        return None

    print(f"{LIGHT_GREEN}[gpu_worker] served by laptop GPU ({len(response.content)} bytes){RESET}", flush=True)
    return response.content
