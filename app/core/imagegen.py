"""
imagegen.py — image generation via the self-hosted "Filigree-Anima"
checkpoint, running on Modal's cloud GPUs.

This process never touches the GPU or the checkpoint file itself -- it just
calls into the already-deployed Modal app over Modal's Python client, the
same shape as core/gpu_worker.py RPCing into the laptop's NVENC worker.
`modal deploy app/modal_imagegen/app.py` has to be run at least once (and
again whenever the checkpoint changes) before this will find anything to
call -- see modal_imagegen/README.md.
"""

import os
import hashlib
import queue
import threading
import time
import modal
from core.config import MODAL_APP_NAME, MODAL_CLS_NAME
from core.colors import *

# Relative to cwd (app/), same convention as MEMORY_FILE etc. in core/config.py.
# Not itself in config.py since nothing else needs to override it -- but
# pulled into a constant (rather than inline string literals below) so
# webui_server.py's image-serving route can import the exact same path
# instead of duplicating it and risking drift.
IMAGES_DIR = "data/images"

_cls = None


def _get_cls():
    global _cls
    if _cls is None:
        _cls = modal.Cls.from_name(MODAL_APP_NAME, MODAL_CLS_NAME)
    return _cls


def generate_image(
    prompt: str,
    negative_prompt: str = "",
    width: int = None,
    height: int = None,
    steps: int = None,
    cfg: float = None,
    seed: int = None,
):
    """
    Generate an image from a text prompt.

    Sync generator -- yields {"type": "progress", "step": N, "total": M} as
    the Modal app reports sampling progress, then a final
    {"type": "done", "path": filename} once the image is saved to
    data/images/, or {"type": "error", "message": str} on failure.

    This blocks on network I/O between yields (Modal's remote_gen() is a
    plain blocking generator, not an async one, even from async callers --
    confirmed directly rather than assumed). Callers on an event loop (e.g.
    Discord) should drive this from a worker thread instead of iterating it
    directly -- see the /imagine command in LapplandV2.py for the thread +
    queue bridge.

    width/height/steps/cfg/seed default to whatever workflow_template.json
    has -- only override when the caller actually wants something different.
    seed=None means the Modal app picks a random one each call (see app.py);
    passing an explicit seed makes the generation reproducible.
    """
    image_bytes = None
    try:
        for update in _get_cls()().generate.remote_gen(
            prompt=prompt,
            negative_prompt=negative_prompt,
            width=width,
            height=height,
            steps=steps,
            cfg=cfg,
            seed=seed,
        ):
            if update["type"] == "progress":
                yield update
            elif update["type"] == "done":
                image_bytes = update["image"]
    except Exception as e:
        print(f"{RED}[imagegen] Modal call failed: {e}{RESET}", flush=True)
        yield {"type": "error", "message": str(e)}
        return

    os.makedirs(IMAGES_DIR, exist_ok=True)
    slug = hashlib.md5(prompt.encode()).hexdigest()[:8]
    filename = f"{IMAGES_DIR}/{slug}_{int(time.time())}.png"
    with open(filename, "wb") as f:
        f.write(image_bytes)

    print(f"{LIGHT_GREEN}[imagegen] generated: {filename}{RESET}", flush=True)
    yield {"type": "done", "path": filename}


# ── Job queue ─────────────────────────────────────────────────────────────
# The deployed Modal container (see modal_imagegen/app.py) only ever runs one
# ComfyUI prompt at a time -- there's no in-container concurrency. Without a
# queue here, several people running /imagine at once would each spin up
# their own container (Modal auto-scales on concurrent calls), meaning
# duplicate cold starts and duplicate GPU billing for work that has to happen
# one at a time on the ComfyUI side regardless. A single persistent worker
# thread pulling off a FIFO queue keeps concurrent requests sharing one warm
# container and gives callers an honest queue position to show the user.
_job_queue: "queue.Queue" = queue.Queue()
_worker_thread = None
_worker_lock = threading.Lock()

# Jobs queued *and* the one currently being processed -- plain qsize() only
# counts jobs still waiting, so a request arriving while the worker is mid-job
# (queue empty, one job in flight) would otherwise be told position 0 /
# "starts immediately" when a job is actually still ahead of it.
_pending = 0
_pending_lock = threading.Lock()


def _worker():
    global _pending
    while True:
        prompt, kwargs, update_queue = _job_queue.get()
        try:
            for update in generate_image(prompt, **kwargs):
                update_queue.put(update)
        except Exception as e:
            update_queue.put({"type": "error", "message": str(e)})
        finally:
            update_queue.put(None)  # sentinel: done
            _job_queue.task_done()
            with _pending_lock:
                _pending -= 1


def _ensure_worker():
    global _worker_thread
    with _worker_lock:
        if _worker_thread is None or not _worker_thread.is_alive():
            _worker_thread = threading.Thread(target=_worker, daemon=True)
            _worker_thread.start()


def enqueue_generate_image(
    prompt: str,
    negative_prompt: str = "",
    width: int = None,
    height: int = None,
    steps: int = None,
    cfg: float = None,
    seed: int = None,
):
    """
    Queue an image-generation job instead of calling generate_image()
    directly. Jobs run strictly one at a time, FIFO, on a shared worker
    thread -- see the module note above for why.

    Returns (update_queue, position):
      - update_queue: a plain queue.Queue the caller should poll (e.g. via
        asyncio.to_thread(update_queue.get)) for the same
        {"type": "progress"/"done"/"error"} updates generate_image() yields,
        terminated by a None sentinel.
      - position: how many jobs were already ahead of this one (0 means it
        starts immediately).
    """
    global _pending
    _ensure_worker()
    update_queue: "queue.Queue" = queue.Queue()
    with _pending_lock:
        position = _pending
        _pending += 1
    _job_queue.put((
        prompt,
        dict(negative_prompt=negative_prompt, width=width, height=height, steps=steps, cfg=cfg, seed=seed),
        update_queue,
    ))
    return update_queue, position
