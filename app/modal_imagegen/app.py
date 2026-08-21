"""
modal_imagegen/app.py -- image generation on Modal's cloud GPUs, using the
custom "Filigree-Anima" checkpoint (a Cosmos-Predict2-2B finetune with a
Qwen3-0.6B text encoder swapped in) plus its paired Qwen-Image VAE.

This runs actual ComfyUI inside the Modal container rather than hand-rolling
a diffusers pipeline. Anima's text-encoder swap needed real architecture
surgery (a custom projection layer + a nonstandard norm parameterization) to
make a Qwen3 encoder work with a transformer trained for a different one --
ComfyUI already has that wiring implemented and tested, so this drives
ComfyUI's own model-loading code over its HTTP API instead of guessing at
that wiring from scratch. This process only ever talks to ComfyUI's API over
loopback; nothing here reimplements the model.

workflow_template.json is a real exported ComfyUI workflow (API format) --
the source of truth for sampler/scheduler/cfg/steps/resolution. At call time
this only overwrites the three model filenames (in case the config paths
change), the prompt text, and the seed; everything else in the template is
used as exported. Re-export and overwrite that file to change sampling
settings rather than hardcoding a second copy here.

Every generated image is run through CompVis/stable-diffusion-safety-checker
(the standard NSFW/CSAM screen Stable Diffusion pipelines have shipped with
for years -- a CLIP image encoder compared against a fixed set of unsafe
"concept" embeddings) before it's returned; a flagged image is never written
to disk or handed back to the caller, just replaced with an error. The
model's weights are baked into the image at build time (see the
`run_commands` python snippet below) so this adds no network round-trip at
call time, only the classification pass itself.

ANIMA_MODEL_PATH / QWEN_TEXT_ENCODER_PATH / QWEN_VAE_PATH point at local
files -- personal downloads, not a hosted model, so there's no API to call,
just files to read. They get baked into the container image at deploy time
via `add_local_file(..., copy=True)` at the paths ComfyUI expects
(models/diffusion_models, models/text_encoders, models/vae), so there's no
separate "upload the model" step: `modal deploy` always ships whatever's
currently at those paths.

This lives under app/ (rather than the repo root) specifically so it's
visible inside the bot's Docker container -- docker-compose only bind-mounts
./app to /app, so anything outside app/ doesn't exist from the container's
point of view.

Usage
-----
    modal run app/modal_imagegen/app.py --prompt "a filigree knight, dramatic lighting"

Deploy as a standing app (core/imagegen.py calls into this by name, so it
needs to be deployed at least once, and redeployed whenever the checkpoint or
workflow_template.json changes, before /imagine will work):
    modal deploy app/modal_imagegen/app.py
"""

import json
import sys
import time
import uuid
from pathlib import Path

# core isn't a Python package this script sits inside of (it's a sibling
# folder, not a parent), so it has to go on sys.path explicitly. This runs
# locally when `modal run`/`modal deploy` first evaluate this file, and
# add_local_python_source() below ships core into the container so the same
# import also resolves when Modal reconstructs this module remotely.
APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))
from core.config import (  # noqa: E402
    ANIMA_MODEL_PATH,
    QWEN_TEXT_ENCODER_PATH,
    QWEN_VAE_PATH,
    MODAL_APP_NAME,
)

import modal  # noqa: E402

LOCAL_UNET     = APP_DIR / ANIMA_MODEL_PATH
LOCAL_CLIP     = APP_DIR / QWEN_TEXT_ENCODER_PATH
LOCAL_VAE      = APP_DIR / QWEN_VAE_PATH
LOCAL_TEMPLATE = Path(__file__).resolve().parent / "workflow_template.json"

# This whole module gets re-imported inside the remote container too (see
# note above), where __file__ points at wherever Modal mounted app.py rather
# than its real repo-relative location -- APP_DIR/LOCAL_* are nonsense there.
# The files themselves are already guaranteed present remotely (that's what
# add_local_file baked in), so this existence check is only meaningful, and
# only safe to run, locally.
if modal.is_local():
    for _f in (LOCAL_UNET, LOCAL_CLIP, LOCAL_VAE, LOCAL_TEMPLATE):
        if not _f.exists():
            sys.exit(f"missing file: {_f}")

GPU_TYPE = "A100-40GB"
COMFYUI_DIR = "/root/ComfyUI"

# Node IDs in workflow_template.json -- fixed as long as the template isn't
# re-exported with a different graph.
NODE_UNET     = "60:44"  # Load Diffusion Model
NODE_CLIP     = "60:45"  # Load CLIP
NODE_VAE      = "60:15"  # Load VAE
NODE_POSITIVE = "60:11"  # CLIP Text Encode (Positive Prompt)
NODE_NEGATIVE = "60:12"  # CLIP Text Encode (Negative Prompt)
NODE_SAMPLER  = "60:19"  # KSampler
NODE_LATENT   = "60:28"  # Empty Latent Image
NODE_SAVE     = "46"     # Save Image

REMOTE_TEMPLATE_PATH = f"{COMFYUI_DIR}/anima_workflow_template.json"

# CompVis/stable-diffusion-safety-checker's weights, cached into the image at
# build time (see the run_commands step below) -- SAFETY_CHECKER_ID is baked
# in rather than read from core/config.py since it's a fixed, well-known
# model id, not a deployment-specific setting.
SAFETY_CHECKER_ID = "CompVis/stable-diffusion-safety-checker"

app = modal.App(MODAL_APP_NAME)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git")
    .run_commands(f"git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git {COMFYUI_DIR}")
    .run_commands(f"pip install -r {COMFYUI_DIR}/requirements.txt")
    .pip_install("requests", "websocket-client", "diffusers", "transformers", "numpy", "pillow")
    .run_commands(
        "python -c \""
        "from transformers import CLIPImageProcessor; "
        "from diffusers.pipelines.stable_diffusion.safety_checker import StableDiffusionSafetyChecker; "
        f"CLIPImageProcessor.from_pretrained('{SAFETY_CHECKER_ID}'); "
        f"StableDiffusionSafetyChecker.from_pretrained('{SAFETY_CHECKER_ID}')\""
    )
    .add_local_python_source("core", copy=True)
    .add_local_file(str(LOCAL_UNET), f"{COMFYUI_DIR}/models/diffusion_models/{LOCAL_UNET.name}", copy=True)
    .add_local_file(str(LOCAL_CLIP), f"{COMFYUI_DIR}/models/text_encoders/{LOCAL_CLIP.name}", copy=True)
    .add_local_file(str(LOCAL_VAE), f"{COMFYUI_DIR}/models/vae/{LOCAL_VAE.name}", copy=True)
    .add_local_file(str(LOCAL_TEMPLATE), REMOTE_TEMPLATE_PATH, copy=True)
)


@app.cls(gpu=GPU_TYPE, image=image, timeout=600, scaledown_window=5)
class AnimaImageGen:
    @modal.enter()
    def load(self):
        import subprocess

        import requests
        from diffusers.pipelines.stable_diffusion.safety_checker import StableDiffusionSafetyChecker
        from transformers import CLIPImageProcessor

        with open(REMOTE_TEMPLATE_PATH) as f:
            self.template = json.load(f)

        # Point the template at whatever's actually baked into this image,
        # rather than trusting the filenames it happened to be exported with.
        self.template[NODE_UNET]["inputs"]["unet_name"] = LOCAL_UNET.name
        self.template[NODE_CLIP]["inputs"]["clip_name"] = LOCAL_CLIP.name
        self.template[NODE_VAE]["inputs"]["vae_name"] = LOCAL_VAE.name

        # Loaded once per warm container and reused across generate() calls,
        # same as ComfyUI itself below -- weights are already cached in the
        # image (see the run_commands step in `image` above), so this is a
        # local read, not a download.
        self.safety_feature_extractor = CLIPImageProcessor.from_pretrained(SAFETY_CHECKER_ID)
        self.safety_checker = StableDiffusionSafetyChecker.from_pretrained(SAFETY_CHECKER_ID)
        self.safety_checker.eval()

        self.session = requests.Session()
        self.proc = subprocess.Popen(
            ["python", "main.py", "--listen", "127.0.0.1", "--port", "8188"],
            cwd=COMFYUI_DIR,
        )

        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                if self.session.get("http://127.0.0.1:8188/system_stats", timeout=5).ok:
                    return
            except requests.exceptions.RequestException:
                pass
            if self.proc.poll() is not None:
                raise RuntimeError(f"ComfyUI exited early (code {self.proc.returncode})")
            time.sleep(1)
        raise RuntimeError("ComfyUI didn't come up within 120s")

    @modal.method()
    def generate(
        self,
        prompt: str,
        negative_prompt: str = "",
        width: int = None,
        height: int = None,
        steps: int = None,
        cfg: float = None,
        seed: int = None,
    ):
        """
        Generator: yields {"type": "progress", "step": N, "total": M} as
        ComfyUI reports sampling progress over its own websocket, then a
        final {"type": "done", "image": bytes}. Callers consume this via
        `.remote_gen()` (an async generator on the client side) instead of
        the single-shot `.remote()` used elsewhere in this repo, since a plain
        `.remote()` call only returns once at the very end -- there'd be
        nothing to render a progress bar from.
        """
        import copy
        import json as json_
        import random

        import websocket

        workflow = copy.deepcopy(self.template)
        workflow[NODE_POSITIVE]["inputs"]["text"] = prompt
        if negative_prompt:
            workflow[NODE_NEGATIVE]["inputs"]["text"] = negative_prompt
        if width:
            workflow[NODE_LATENT]["inputs"]["width"] = width
        if height:
            workflow[NODE_LATENT]["inputs"]["height"] = height
        if steps:
            workflow[NODE_SAMPLER]["inputs"]["steps"] = steps
        if cfg:
            workflow[NODE_SAMPLER]["inputs"]["cfg"] = cfg
        workflow[NODE_SAMPLER]["inputs"]["seed"] = seed if seed is not None else random.randint(0, 2**63 - 1)

        client_id = str(uuid.uuid4())

        ws = websocket.WebSocket()
        ws.connect(f"ws://127.0.0.1:8188/ws?clientId={client_id}", timeout=30)
        ws.settimeout(30)

        try:
            resp = self.session.post(
                "http://127.0.0.1:8188/prompt",
                json={"prompt": workflow, "client_id": client_id},
                timeout=30,
            )
            resp.raise_for_status()
            prompt_id = resp.json()["prompt_id"]

            deadline = time.time() + 480
            while time.time() < deadline:
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not isinstance(raw, str):
                    continue  # binary frames are live preview images -- skip
                msg = json_.loads(raw)
                data = msg.get("data", {})
                if data.get("prompt_id") not in (None, prompt_id):
                    continue  # this container only runs one job at a time, but be safe
                if msg.get("type") == "progress" and "value" in data and "max" in data:
                    yield {"type": "progress", "step": data["value"], "total": data["max"]}
                elif msg.get("type") == "executing" and data.get("node") is None:
                    break  # workflow finished
            else:
                raise RuntimeError(f"ComfyUI didn't finish prompt {prompt_id} within 480s")
        finally:
            ws.close()

        resp = self.session.get(f"http://127.0.0.1:8188/history/{prompt_id}", timeout=30)
        resp.raise_for_status()
        history = resp.json()[prompt_id]

        status = history.get("status", {})
        if status.get("status_str") == "error":
            raise RuntimeError(f"ComfyUI reported an error for prompt {prompt_id}: {status}")

        image_info = history["outputs"][NODE_SAVE]["images"][0]
        resp = self.session.get(
            "http://127.0.0.1:8188/view",
            params={
                "filename": image_info["filename"],
                "subfolder": image_info["subfolder"],
                "type": image_info["type"],
            },
            timeout=30,
        )
        resp.raise_for_status()

        if self._is_nsfw(resp.content):
            # Deliberately never yields the image bytes in this case -- the
            # caller (core/imagegen.py) only sees an error, same as any other
            # generation failure, so a flagged image is never written to
            # data/images/ or handed back to whoever asked for it.
            raise RuntimeError("flagged as NSFW by the safety filter")

        yield {"type": "done", "image": resp.content}

    def _is_nsfw(self, image_bytes: bytes) -> bool:
        import io

        import numpy as np
        from PIL import Image

        pil_image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        np_image = np.array(pil_image).astype("float32") / 255.0
        clip_input = self.safety_feature_extractor(pil_image, return_tensors="pt").pixel_values

        _, has_nsfw_concept = self.safety_checker(images=[np_image], clip_input=clip_input)
        return bool(has_nsfw_concept[0])


@app.local_entrypoint()
def main(
    prompt: str,
    negative_prompt: str = "",
    width: int = None,
    height: int = None,
    steps: int = None,
    cfg: float = None,
    seed: int = None,
    output: str = None,
):
    # remote_gen() returns a plain (blocking) generator, not an async one --
    # fine here since local_entrypoint has nothing else to interleave with.
    image_bytes = None
    for update in AnimaImageGen().generate.remote_gen(
        prompt=prompt,
        negative_prompt=negative_prompt,
        width=width,
        height=height,
        steps=steps,
        cfg=cfg,
        seed=seed,
    ):
        if update["type"] == "progress":
            print(f"\r{update['step']}/{update['total']}", end="", flush=True)
        elif update["type"] == "done":
            image_bytes = update["image"]
    print()

    out_path = Path(output) if output else Path("data/images") / f"anima_{int(time.time())}.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(image_bytes)
    print(f"saved: {out_path}")
