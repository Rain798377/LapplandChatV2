"""
imagegen.py — image generation via Cloudflare Workers AI

Env vars: CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN
          (dash.cloudflare.com — the token needs the "Workers AI" > Edit permission)
Model:    @cf/black-forest-labs/flux-1-schnell
"""

import os
import base64
import hashlib
import time
import httpx
from core.config import CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN
from core.colors import *

IMAGE_MODEL = "@cf/black-forest-labs/flux-1-schnell"


def generate_image(prompt: str, width: int = 1024, height: int = 1024) -> str | None:
    """
    Generate an image from a text prompt.
    Saves the image to data/images/ and returns the local file path, or None on failure.
    """
    if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN:
        print(f"{RED}[imagegen] CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN not set{RESET}", flush=True)
        return None

    try:
        resp = httpx.post(
            f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/run/{IMAGE_MODEL}",
            headers={"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"},
            json={"prompt": prompt},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()

        if not data.get("success"):
            print(f"{RED}[imagegen] Cloudflare returned an error: {data.get('errors')}{RESET}", flush=True)
            return None

        image_b64 = data.get("result", {}).get("image")
        if not image_b64:
            print(f"{RED}[imagegen] Cloudflare response had no image{RESET}", flush=True)
            return None

        os.makedirs("data/images", exist_ok=True)
        slug = hashlib.md5(prompt.encode()).hexdigest()[:8]
        filename = f"data/images/{slug}_{int(time.time())}.png"
        with open(filename, "wb") as f:
            f.write(base64.b64decode(image_b64))

        print(f"{LIGHT_GREEN}[imagegen] generated: {filename}{RESET}", flush=True)
        return filename

    except Exception as e:
        print(f"{RED}[imagegen] failed: {e}{RESET}", flush=True)
        return None
