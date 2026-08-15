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
import time
import modal
from core.config import MODAL_APP_NAME, MODAL_CLS_NAME
from core.colors import *

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

    width/height/steps/cfg default to whatever workflow_template.json has --
    only override when the caller actually wants something different.
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
        ):
            if update["type"] == "progress":
                yield update
            elif update["type"] == "done":
                image_bytes = update["image"]
    except Exception as e:
        print(f"{RED}[imagegen] Modal call failed: {e}{RESET}", flush=True)
        yield {"type": "error", "message": str(e)}
        return

    os.makedirs("data/images", exist_ok=True)
    slug = hashlib.md5(prompt.encode()).hexdigest()[:8]
    filename = f"data/images/{slug}_{int(time.time())}.png"
    with open(filename, "wb") as f:
        f.write(image_bytes)

    print(f"{LIGHT_GREEN}[imagegen] generated: {filename}{RESET}", flush=True)
    yield {"type": "done", "path": filename}
