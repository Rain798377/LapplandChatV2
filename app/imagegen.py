"""
imagegen.py — image generation via fal.ai (FLUX)

Install: pip install fal-client
Env var:  FAL_KEY  (get one free at fal.ai)
"""

import os
import fal_client
from colors import *
from config import FAL_KEY

# Model options (swap freely):
#   fal-ai/flux/schnell   — fast, free tier friendly
#   fal-ai/flux/dev       — higher quality, slower
IMAGEGEN_MODEL = "fal-ai/flux/schnell"


def generate_image(prompt: str, width: int = 1024, height: int = 1024) -> str | None:
    """
    Generate an image from a text prompt.
    Returns the image URL string, or None on failure.
    """
    if not FAL_KEY:
        print(f"{RED}[imagegen] FAL_KEY not set{RESET}", flush=True)
        return None

    os.environ["FAL_KEY"] = FAL_KEY  # fal_client reads this automatically

    try:
        result = fal_client.run(
            IMAGEGEN_MODEL,
            arguments={
                "prompt": prompt,
                "image_size": {"width": width, "height": height},
                "num_inference_steps": 4,   # schnell is good at 4 steps
                "num_images": 1,
            },
        )
        url = result["images"][0]["url"]
        print(f"{LIGHT_GREEN}[imagegen] generated: {url}{RESET}", flush=True)
        return url
    except Exception as e:
        print(f"{RED}[imagegen] failed: {e}{RESET}", flush=True)
        return None