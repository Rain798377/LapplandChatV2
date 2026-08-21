"""
imagegen.py — image generation via self-hosted checkpoints running on
Modal's cloud GPUs: "Filigree-Anima" ("anima", the default) and
FLUX.1-schnell ("flux") -- see IMAGEGEN_BACKENDS in core/config.py.

This process never touches the GPU or the checkpoint file itself -- it just
calls into the already-deployed Modal app over Modal's Python client, the
same shape as core/gpu_worker.py RPCing into the laptop's NVENC worker.
`modal deploy app/modal_imagegen/app.py` (anima) / `modal deploy
app/modal_flux/app.py` (flux) each have to be run at least once (and again
whenever their checkpoint/weights change) before that backend will find
anything to call -- see modal_imagegen/README.md and modal_flux/README.md.
"""

import os
import hashlib
import queue
import threading
import time
import modal
from core.config import IMAGEGEN_BACKENDS
from core.colors import *

# Relative to cwd (app/), same convention as MEMORY_FILE etc. in core/config.py.
# Not itself in config.py since nothing else needs to override it -- but
# pulled into a constant (rather than inline string literals below) so
# webui_server.py's image-serving route can import the exact same path
# instead of duplicating it and risking drift.
IMAGES_DIR = "data/images"

DEFAULT_BACKEND = "anima"

# One modal.Cls per backend, resolved lazily and cached -- each backend is
# its own deployed Modal app (see IMAGEGEN_BACKENDS), not interchangeable
# instances of the same one.
_cls_cache: dict[str, "modal.Cls"] = {}


def _get_cls(model: str):
    if model not in _cls_cache:
        app_name, cls_name = IMAGEGEN_BACKENDS[model]
        _cls_cache[model] = modal.Cls.from_name(app_name, cls_name)
    return _cls_cache[model]


def generate_image(
    prompt: str,
    negative_prompt: str = "",
    width: int = None,
    height: int = None,
    steps: int = None,
    cfg: float = None,
    seed: int = None,
    model: str = DEFAULT_BACKEND,
):
    """
    Generate an image from a text prompt via the given backend (a key in
    IMAGEGEN_BACKENDS, e.g. "anima" or "flux").

    Sync generator -- yields {"type": "progress", "step": N, "total": M} as
    the Modal app reports sampling progress, then a final
    {"type": "done", "path": filename} once the image is saved to
    data/images/, or {"type": "error", "message": str} on failure.

    This blocks on network I/O between yields (Modal's remote_gen() is a
    plain blocking generator, not an async one, even from async callers --
    confirmed directly rather than assumed). Callers on an event loop (e.g.
    Discord) should drive this from a worker thread instead of iterating it
    directly -- see the /imagine commands in LapplandV2.py for the thread +
    queue bridge.

    width/height/steps/cfg/seed default to whatever the backend's own
    workflow template has -- only override when the caller actually wants
    something different. seed=None means the Modal app picks a random one
    each call; passing an explicit seed makes the generation reproducible.
    negative_prompt is accepted for every backend but has no effect on
    "flux" (FLUX.1-schnell is CFG-distilled -- see modal_flux/app.py).
    """
    if model not in IMAGEGEN_BACKENDS:
        yield {"type": "error", "message": f"unknown image-gen backend '{model}'"}
        return

    image_bytes = None
    try:
        for update in _get_cls(model)().generate.remote_gen(
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
        print(f"{RED}[imagegen] Modal call failed ({model}): {e}{RESET}", flush=True)
        yield {"type": "error", "message": str(e)}
        return

    os.makedirs(IMAGES_DIR, exist_ok=True)
    slug = hashlib.md5(prompt.encode()).hexdigest()[:8]
    filename = f"{IMAGES_DIR}/{model}_{slug}_{int(time.time())}.png"
    with open(filename, "wb") as f:
        f.write(image_bytes)

    print(f"{LIGHT_GREEN}[imagegen] generated ({model}): {filename}{RESET}", flush=True)
    yield {"type": "done", "path": filename}


# ── Job queues ────────────────────────────────────────────────────────────
# Each deployed Modal container (see modal_imagegen/app.py, modal_flux/app.py)
# only ever runs one ComfyUI prompt at a time -- there's no in-container
# concurrency. Without a queue, several people running the same backend's
# /imagine at once would each spin up their own container (Modal auto-scales
# on concurrent calls), meaning duplicate cold starts and duplicate GPU
# billing for work that has to happen one at a time on the ComfyUI side
# regardless. A persistent worker thread per backend, each pulling off its
# own FIFO queue, keeps concurrent requests to the *same* backend sharing one
# warm container and gives callers an honest queue position -- while still
# letting two different backends (anima, flux) run concurrently, since they're
# genuinely independent containers with no shared resource to serialize on.
_job_queues: dict[str, "queue.Queue"] = {}
_worker_threads: dict[str, threading.Thread] = {}
_worker_lock = threading.Lock()

# Jobs queued *and* the one currently being processed, per backend -- plain
# qsize() only counts jobs still waiting, so a request arriving while the
# worker is mid-job (queue empty, one job in flight) would otherwise be told
# position 0 / "starts immediately" when a job is actually still ahead of it.
_pending: dict[str, int] = {}
_pending_lock = threading.Lock()


def _worker(model: str):
    q = _job_queues[model]
    while True:
        prompt, kwargs, update_queue = q.get()
        try:
            for update in generate_image(prompt, **kwargs):
                update_queue.put(update)
        except Exception as e:
            update_queue.put({"type": "error", "message": str(e)})
        finally:
            update_queue.put(None)  # sentinel: done
            q.task_done()
            with _pending_lock:
                _pending[model] -= 1


def _ensure_worker(model: str):
    with _worker_lock:
        if model not in _job_queues:
            _job_queues[model] = queue.Queue()
            _pending[model] = 0
        t = _worker_threads.get(model)
        if t is None or not t.is_alive():
            t = threading.Thread(target=_worker, args=(model,), daemon=True)
            _worker_threads[model] = t
            t.start()


def enqueue_generate_image(
    prompt: str,
    negative_prompt: str = "",
    width: int = None,
    height: int = None,
    steps: int = None,
    cfg: float = None,
    seed: int = None,
    model: str = DEFAULT_BACKEND,
):
    """
    Queue an image-generation job instead of calling generate_image()
    directly. Jobs for the same backend run strictly one at a time, FIFO, on
    that backend's own worker thread -- see the module note above for why,
    and for why two different backends don't serialize against each other.

    Returns (update_queue, position):
      - update_queue: a plain queue.Queue the caller should poll (e.g. via
        asyncio.to_thread(update_queue.get)) for the same
        {"type": "progress"/"done"/"error"} updates generate_image() yields,
        terminated by a None sentinel.
      - position: how many jobs for this backend were already ahead of this
        one (0 means it starts immediately).
    """
    _ensure_worker(model)
    update_queue: "queue.Queue" = queue.Queue()
    with _pending_lock:
        position = _pending[model]
        _pending[model] += 1
    _job_queues[model].put((
        prompt,
        dict(negative_prompt=negative_prompt, width=width, height=height, steps=steps, cfg=cfg, seed=seed, model=model),
        update_queue,
    ))
    return update_queue, position
