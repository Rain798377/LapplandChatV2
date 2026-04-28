"""
imagegen.py — image generation via Hugging Face Inference Providers

Install: pip install huggingface_hub pillow
Env var:  HF_TOKEN  (huggingface.co/settings/tokens — needs "Make calls to Inference Providers" permission)

Provider options (set IMAGEGEN_PROVIDER):
  "fal-ai"    — FLUX.1-schnell, fast, good free tier
  "replicate" — alternative if fal-ai quota runs out
  "together"  — another solid option

Model options (set IMAGEGEN_MODEL):
  black-forest-labs/FLUX.1-schnell  — fast, free tier friendly
  black-forest-labs/FLUX.1-dev      — higher quality, slower
  ByteDance/SDXL-Lightning          — fast SDXL alternative
"""

import os
import hashlib
import time
from huggingface_hub import InferenceClient
from colors import *

HF_TOKEN = None
try:
    from config import HF_TOKEN
except ImportError:
    pass

HF_TOKEN = HF_TOKEN or os.environ.get("HF_TOKEN")

IMAGEGEN_PROVIDER = "fal-ai"
IMAGEGEN_MODEL    = "black-forest-labs/FLUX.1-schnell"


def generate_image(prompt: str, width: int = 1024, height: int = 1024) -> str | None:
    """
    Generate an image from a text prompt.
    Saves the image to data/images/ and returns the local file path, or None on failure.
    """
    if not HF_TOKEN:
        print(f"{RED}[imagegen] HF_TOKEN not set{RESET}", flush=True)
        return None

    try:
        client = InferenceClient(
            provider=IMAGEGEN_PROVIDER,
            api_key=HF_TOKEN,
        )

        # Returns a PIL.Image object
        image = client.text_to_image(
            prompt,
            model=IMAGEGEN_MODEL,
            width=width,
            height=height,
        )

        os.makedirs("data/images", exist_ok=True)
        slug = hashlib.md5(prompt.encode()).hexdigest()[:8]
        filename = f"data/images/{slug}_{int(time.time())}.png"
        image.save(filename)

        print(f"{LIGHT_GREEN}[imagegen] generated: {filename}{RESET}", flush=True)
        return filename

    except Exception as e:
        print(f"{RED}[imagegen] failed: {e}{RESET}", flush=True)
        return None