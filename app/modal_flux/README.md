# modal_flux

Image generation using `black-forest-labs/FLUX.1-schnell`, run on Modal's
cloud GPUs -- a second backend alongside `modal_imagegen/` (Anima), not a
replacement. `core/imagegen.py` doesn't call into this yet; nothing wires it
into `/imagine` until that's asked for.

Runs the same way `modal_imagegen/` runs Anima: real ComfyUI inside the
container, driven over its own HTTP API, not a hand-rolled diffusers
pipeline. See that directory's README for the shared reasoning (why ComfyUI
over diffusers, why a real container over an API call). What's different
here is where the weights live -- see below.

## Weights: a Modal Volume, not baked into the image

Anima is a personal checkpoint that already lives on local disk and gets
baked into the image at deploy time. FLUX.1-schnell is a public ~30GB+ Hub
repo; routing that through a home connection (download locally, then
re-upload to Modal at deploy time) wastes both legs of that transfer for no
reason. Instead its weights live in a `modal.Volume` named
`flux-schnell-weights`, populated by a Modal function that runs `hf download`
*inside* Modal -- Hub to Modal, datacenter-to-datacenter, never touching
local disk.

**Only the ComfyUI-loadable files are fetched**, not either repo in full:

From `black-forest-labs/FLUX.1-schnell`:
```
flux1-schnell.safetensors   # the 12B transformer/UNet
ae.safetensors               # the VAE
```
That repo ships these at the repo root *and* ships a second, separate
diffusers-format layout (`transformer/`, `vae/`, `text_encoder/`,
`text_encoder_2/`, `tokenizer*/`, `scheduler/`, `model_index.json`) that
ComfyUI doesn't load and this setup never touches.

From `comfyanonymous/flux_text_encoders` (the ComfyUI creator's own repo,
hosting the single-file DualCLIPLoader-ready versions -- FLUX.1-schnell's
own `text_encoder*/` folders are diffusers-sharded, not this format):
```
clip_l.safetensors
t5xxl_fp8_e4m3fn.safetensors
```

## Setup

1. `pip install modal` (already in requirements.txt) and `modal setup` to
   authenticate, if not already done.
2. black-forest-labs gates every repo behind a click-through access approval
   even under an open license (FLUX.1-schnell is Apache-2.0, but still
   gated -- `comfyanonymous/flux_text_encoders` is not) -- accept the terms
   at https://huggingface.co/black-forest-labs/FLUX.1-schnell with the HF
   account whose token you'll use, then create a Modal secret from that
   account's token:
   ```
   modal secret create huggingface-secret HF_TOKEN=hf_...
   ```
   `download_weights()` reads this secret for both repos' downloads (sent
   even to the ungated one -- harmless, and one secret is simpler than
   conditionally attaching it per repo); `hf download` picks up `HF_TOKEN`
   from the environment automatically, no other config needed.
3. Fetch all four files into the volume (one-time, or whenever you want to
   force a re-check):
   ```
   modal run app/modal_flux/app.py::download_weights
   ```
   Safe to re-run -- `hf download` resumes/skips files it already has.
4. Deploy:
   ```
   modal deploy app/modal_flux/app.py
   ```

## workflow_template.json: hand-authored, not exported

`modal_imagegen/workflow_template.json` (Anima) is a real workflow exported
from a live ComfyUI session -- the actual source of truth for that pipeline.
This one isn't: it's built from ComfyUI's well-documented official
FLUX.1-schnell example graph (`UNETLoader -> DualCLIPLoader -> VAELoader ->
CLIPTextEncode -> EmptySD3LatentImage -> KSampler -> VAEDecode ->
SaveImage`), since a live FLUX ComfyUI session wasn't available to export
from. The negative-prompt wiring (pointed at the same CLIPTextEncode node as
positive) is deliberate, not a placeholder -- schnell is CFG-distilled
(`cfg=1` by convention), and at `cfg=1` ComfyUI's sampler always evaluates to
exactly the positive prediction regardless of what negative points at.
Treat the rest as a solid starting point, not a guarantee: verify it (or
replace it with a real export) before relying on it, especially if
generation produces something unexpected once the text encoders are in
place.

## Behavior

- GPU: `A100-40GB`, same as Anima (`GPU_TYPE` in `app.py`).
- Defaults are schnell's own distillation regime: `steps=4`, `cfg=1`.
  Raising either doesn't improve output the way it would for a standard
  (non-distilled) diffusion model.
- Every generated image is screened by
  `CompVis/stable-diffusion-safety-checker` before it's returned, same as
  Anima -- a flagged image is never written to disk or handed back to the
  caller.
- Containers scale to zero after 5s idle (`scaledown_window`), same
  tradeoff as Anima: near-zero idle cost, a cold start on most requests.
