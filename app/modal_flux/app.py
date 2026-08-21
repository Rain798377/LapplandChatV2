"""
modal_flux/app.py -- image generation on Modal's cloud GPUs using
black-forest-labs/FLUX.1-schnell, run the same way modal_imagegen/app.py
runs the "Filigree-Anima" checkpoint: real ComfyUI inside the container,
driven over its own HTTP API rather than a hand-rolled diffusers pipeline.

Weight storage differs from Anima on purpose. Anima is a personal checkpoint
that already lives on local disk and gets baked into the image at deploy
time (see modal_imagegen/app.py) -- there's no "download" step, just files to
read. FLUX.1-schnell is a public ~30GB+ Hub repo; routing that through a home
connection (download locally, then re-upload to Modal at deploy time) wastes
both legs of that transfer for no reason. Instead its weights live in a
modal.Volume, populated once by download_weights() below pulling directly
from the Hub into Modal (datacenter-to-datacenter), and FluxImageGen.load()
symlinks whatever's in the volume into ComfyUI's expected model folders at
container start.

Only two files are fetched from FLUX_REPO_ID -- black-forest-labs/FLUX.1-schnell
ships both a diffusers-format layout (transformer/, vae/, text_encoder/,
text_encoder_2/, tokenizer*/, scheduler/, model_index.json) AND the
single-file checkpoints ComfyUI actually loads, sitting at the repo root:
    flux1-schnell.safetensors  -- the 12B transformer/UNet
    ae.safetensors             -- the VAE
FLUX_FILES below is exactly those two, not the whole repo.

Text encoders (CLIP-L, T5-XXL) come from a second repo, TEXT_ENCODER_REPO_ID
(comfyanonymous/flux_text_encoders -- the ComfyUI creator's own repo hosting
DualCLIPLoader-ready single files) rather than FLUX_REPO_ID's own
text_encoder*/ folders, which are diffusers-sharded, not the single-file
format ComfyUI wants. Unlike FLUX_REPO_ID, this one isn't gated -- no
HF_TOKEN access-approval needed for it specifically, though download_weights()
uses the same secret for both since it's harmless to send on an ungated repo
too.

workflow_template.json here is hand-authored from ComfyUI's well-documented
official FLUX.1-schnell example graph (UNETLoader -> DualCLIPLoader ->
VAELoader -> CLIPTextEncode -> EmptySD3LatentImage -> KSampler -> VAEDecode
-> SaveImage), NOT a live export the way modal_imagegen/workflow_template.json
is -- that one came from an actual ComfyUI session with Anima loaded; this
one didn't, since a live FLUX session wasn't available to export from.
schnell's negative prompt is wired to its own positive CLIPTextEncode output
deliberately, not left blank -- schnell is CFG-distilled (cfg=1 by
convention), and at cfg=1 ComfyUI's KSampler always evaluates to exactly the
positive prediction regardless of what negative points at, so this is a
no-op by construction rather than a placeholder that happens to work.
Treat the rest of the graph as a solid starting point, not a guarantee --
verify (or replace with a real export) before relying on it.

Usage
-----
    modal run app/modal_flux/app.py::download_weights
    modal deploy app/modal_flux/app.py
"""

import json
import sys
import time
import uuid
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))
from core.config import FLUX_MODAL_APP_NAME  # noqa: E402

import modal  # noqa: E402

FLUX_REPO_ID = "black-forest-labs/FLUX.1-schnell"
# The only two files ComfyUI needs -- see module docstring for why the rest
# of the repo (diffusers-format transformer/vae/text_encoder* folders,
# model_index.json) is skipped.
FLUX_FILES = ("flux1-schnell.safetensors", "ae.safetensors")
UNET_FILENAME, VAE_FILENAME = FLUX_FILES

# CLIP-L / T5-XXL text encoders -- not part of black-forest-labs/FLUX.1-schnell
# in single-file form (see module docstring). comfyanonymous/flux_text_encoders
# is the ComfyUI creator's own repo hosting exactly the DualCLIPLoader-ready
# files; unlike FLUX_REPO_ID, it isn't gated, no HF_TOKEN access-approval
# needed for this one.
TEXT_ENCODER_REPO_ID = "comfyanonymous/flux_text_encoders"
TEXT_ENCODER_FILES = ("clip_l.safetensors", "t5xxl_fp8_e4m3fn.safetensors")
CLIP_L_FILENAME, T5XXL_FILENAME = TEXT_ENCODER_FILES

COMFYUI_DIR = "/root/ComfyUI"
VOLUME_MOUNT = "/vol/flux"
LOCAL_TEMPLATE = Path(__file__).resolve().parent / "workflow_template.json"

# Node IDs in workflow_template.json.
NODE_UNET     = "1"
NODE_CLIP     = "2"
NODE_VAE      = "3"
NODE_POSITIVE = "4"
NODE_LATENT   = "5"
NODE_SAMPLER  = "6"
NODE_SAVE     = "8"

# CompVis/stable-diffusion-safety-checker's weights, cached into the image at
# build time -- same as modal_imagegen/app.py, and for the same reason (every
# generated image gets screened before it's ever returned).
SAFETY_CHECKER_ID = "CompVis/stable-diffusion-safety-checker"

app = modal.App(FLUX_MODAL_APP_NAME)

# Weights live here instead of being baked into the image -- see module
# docstring. create_if_missing=True so the very first `modal run
# app/modal_flux/app.py::download_weights` works without a separate
# `modal volume create` step.
flux_volume = modal.Volume.from_name("flux-schnell-weights", create_if_missing=True)

# A separate, much lighter image than the ComfyUI one below -- downloading
# doesn't need ComfyUI, torch, or the safety checker, just the HF client.
# add_local_python_source("core", ...) is still required here even though
# download_weights() itself never touches core -- Modal re-imports this
# whole module (including the top-level `from core.config import ...`) in
# every container regardless of which function it's running.
download_image = (
    modal.Image.debian_slim(python_version="3.11")
    # No "[cli]" extra -- current huggingface_hub ships the `hf` command in
    # the base package (pip warns "does not provide the extra 'cli'" if you
    # ask for it anyway; harmless, but there's no reason to ask).
    .pip_install("huggingface_hub")
    .add_local_python_source("core", copy=True)
)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git")
    .run_commands(f"git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git {COMFYUI_DIR}")
    .run_commands(f"pip install -r {COMFYUI_DIR}/requirements.txt")
    .pip_install("requests", "websocket-client", "diffusers", "transformers", "numpy", "pillow")
    # See modal_imagegen/app.py's identical step for why this is set before
    # the safety-checker download: an HF Hub tqdm progress bar's block
    # characters crashed `modal deploy`'s local log streaming on at least
    # one Windows console encoding.
    .env({"HF_HUB_DISABLE_PROGRESS_BARS": "1"})
    .run_commands(
        "python -c \""
        "from transformers import CLIPImageProcessor; "
        "from diffusers.pipelines.stable_diffusion.safety_checker import StableDiffusionSafetyChecker; "
        f"CLIPImageProcessor.from_pretrained('{SAFETY_CHECKER_ID}'); "
        f"StableDiffusionSafetyChecker.from_pretrained('{SAFETY_CHECKER_ID}')\""
    )
    .add_local_python_source("core", copy=True)
    .add_local_file(str(LOCAL_TEMPLATE), f"{COMFYUI_DIR}/flux_workflow_template.json", copy=True)
)


@app.function(
    image=download_image,
    volumes={VOLUME_MOUNT: flux_volume},
    # black-forest-labs gates every repo behind a click-through access
    # approval even under an open license -- create this secret yourself
    # (`modal secret create huggingface-secret HF_TOKEN=hf_...`) with a
    # token from an account that's accepted the terms at
    # https://huggingface.co/black-forest-labs/FLUX.1-schnell. `hf download`
    # picks up HF_TOKEN from the environment automatically.
    secrets=[modal.Secret.from_name("huggingface-secret")],
    timeout=3600,
)
def download_weights():
    """
    Fetches FLUX_FILES from FLUX_REPO_ID and TEXT_ENCODER_FILES from
    TEXT_ENCODER_REPO_ID straight into the Modal volume -- Hub to Modal,
    never routed through local disk. Safe to re-run: `hf download`
    resumes/skips files it already has instead of starting over.
    """
    import subprocess

    for repo_id, files in ((FLUX_REPO_ID, FLUX_FILES), (TEXT_ENCODER_REPO_ID, TEXT_ENCODER_FILES)):
        subprocess.run(
            ["hf", "download", repo_id, *files, "--local-dir", VOLUME_MOUNT],
            check=True,
        )
        print(f"downloaded {files} from {repo_id} into {VOLUME_MOUNT}")
    flux_volume.commit()


GPU_TYPE = "A100-40GB"


@app.cls(gpu=GPU_TYPE, image=image, volumes={VOLUME_MOUNT: flux_volume}, timeout=600, scaledown_window=5)
class FluxImageGen:
    @modal.enter()
    def load(self):
        import os
        import subprocess

        import requests
        from diffusers.pipelines.stable_diffusion.safety_checker import StableDiffusionSafetyChecker
        from transformers import CLIPImageProcessor

        with open(f"{COMFYUI_DIR}/flux_workflow_template.json") as f:
            self.template = json.load(f)

        # Symlink whatever download_weights() put in the volume into
        # ComfyUI's expected model folders -- not baked into the image (see
        # module docstring), so this runs at every container start instead
        # of once at build time.
        dest_by_name = {
            UNET_FILENAME: f"{COMFYUI_DIR}/models/diffusion_models/{UNET_FILENAME}",
            VAE_FILENAME: f"{COMFYUI_DIR}/models/vae/{VAE_FILENAME}",
            CLIP_L_FILENAME: f"{COMFYUI_DIR}/models/text_encoders/{CLIP_L_FILENAME}",
            T5XXL_FILENAME: f"{COMFYUI_DIR}/models/text_encoders/{T5XXL_FILENAME}",
        }
        for filename, dst in dest_by_name.items():
            src = f"{VOLUME_MOUNT}/{filename}"
            if not os.path.exists(src):
                raise RuntimeError(
                    f"{src} missing from the volume -- run "
                    "`modal run app/modal_flux/app.py::download_weights` first"
                )
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if not os.path.exists(dst):
                os.symlink(src, dst)

        self.template[NODE_UNET]["inputs"]["unet_name"] = UNET_FILENAME
        self.template[NODE_VAE]["inputs"]["vae_name"] = VAE_FILENAME
        self.template[NODE_CLIP]["inputs"]["clip_name1"] = CLIP_L_FILENAME
        self.template[NODE_CLIP]["inputs"]["clip_name2"] = T5XXL_FILENAME

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
        Same shape as AnimaImageGen.generate() (modal_imagegen/app.py) --
        yields {"type": "progress", ...} then {"type": "done", "image": bytes}.
        negative_prompt is accepted for interface parity with Anima but has
        no effect: see module docstring for why that's correct for schnell,
        not an oversight. Defaults (steps=4, cfg=1) match schnell's own
        distillation regime -- raising steps or cfg for a "better" image
        doesn't help this model the way it would a standard diffusion model.
        """
        import copy
        import json as json_
        import random

        import websocket

        workflow = copy.deepcopy(self.template)
        workflow[NODE_POSITIVE]["inputs"]["text"] = prompt
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
    width: int = None,
    height: int = None,
    steps: int = None,
    cfg: float = None,
    seed: int = None,
    output: str = None,
):
    image_bytes = None
    for update in FluxImageGen().generate.remote_gen(
        prompt=prompt, width=width, height=height, steps=steps, cfg=cfg, seed=seed
    ):
        if update["type"] == "progress":
            print(f"\r{update['step']}/{update['total']}", end="", flush=True)
        elif update["type"] == "done":
            image_bytes = update["image"]
    print()

    out_path = Path(output) if output else Path("data/images") / f"flux_{int(time.time())}.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(image_bytes)
    print(f"saved: {out_path}")
