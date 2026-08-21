# modal_imagegen

Image generation using the custom "Filigree-Anima" checkpoint, run on Modal's
cloud GPUs. This is the bot's standard image generator (`core/imagegen.py`
calls into this) -- there's no fallback provider, so `/imagine` won't work
until this is deployed.

Not a Python package in the `core`/`commands`/`services` sense -- it runs as
its own standalone process, executing in Modal's cloud rather than as part of
the bot. It lives under `app/` (not the repo root) specifically so it's
visible inside the bot's Docker container: docker-compose only bind-mounts
`./app` to `/app`, so anything outside `app/` doesn't exist from the
container's point of view, and `modal deploy` needs to run somewhere that can
see the actual checkpoint file.

Internally this runs an actual ComfyUI instance inside the container (started
by `AnimaImageGen.load()`) and drives it over its own HTTP API using
`workflow_template.json` -- a real exported ComfyUI workflow, not a
hand-built diffusers pipeline. Filigree-Anima is a Cosmos-Predict2-2B
finetune with a Qwen3-0.6B text encoder swapped in, which needed real
architecture surgery (a custom projection layer + nonstandard norm) to work;
ComfyUI already has that wiring built and tested, so this reuses it rather
than reimplementing it from scratch. `workflow_template.json` is also the
source of truth for sampler/scheduler/cfg/steps/resolution -- re-export and
overwrite that file to change sampling settings, don't hardcode a second copy
in `app.py` or `config.py`.

The checkpoint, text encoder, and VAE all live under `app/models/` in this
repo (gitignored -- they're large binaries, not something that belongs in
git history). Their local paths are read from `ANIMA_MODEL_PATH` /
`QWEN_TEXT_ENCODER_PATH` / `QWEN_VAE_PATH` in `core/config.py`, and get baked
straight into the deployed container image at deploy time (at the paths
ComfyUI expects: `models/diffusion_models`, `models/text_encoders`,
`models/vae`) -- no separate "upload the model" step to remember.

## Setup

1. `pip install modal` (already in requirements.txt / the Docker image) and
   `modal setup` to authenticate, if not already done -- or set
   `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` as env vars if running
   non-interactively (e.g. inside the container).
2. Deploy (from the repo root, or from `/app` inside the container):
   ```
   modal deploy app/modal_imagegen/app.py
   ```
   This reads the three paths out of `core/config.py`, checks the files
   exist locally, and bundles them into the image (image build also clones
   ComfyUI and installs its own requirements.txt). Whatever's at those paths
   *right now* is what ships -- redeploy after swapping in a newer checkpoint
   or re-exporting `workflow_template.json`.

## Usage

Once deployed, the bot's `/imagine` command calls this automatically via
`core/imagegen.py`. To test directly without going through the bot:
```
modal run app/modal_imagegen/app.py --prompt "a filigree knight, dramatic lighting"
```
Optional flags: `--negative-prompt`, `--width`, `--height`, `--seed`,
`--output <path>` (defaults to `data/images/anima_<timestamp>.png`). Leaving
width/height/seed unset uses whatever `workflow_template.json` has (seed
still gets randomized per call regardless, since reusing the exported seed
would generate the same image every time).

## Behavior

- GPU: `A100-40GB` by default (bump/change `GPU_TYPE` in `app.py` if the
  checkpoint needs more VRAM or you want to trade cost for speed).
- `AnimaImageGen.load()` starts ComfyUI as a subprocess on container start
  and waits for `/system_stats` to respond before accepting jobs; `generate()`
  submits the workflow to ComfyUI's own `/prompt` API and polls `/history`
  for the result, the same way you'd drive ComfyUI's API from any other
  client.
- Containers scale to zero `scaledown_window` (5 seconds) after finishing a
  job -- no idle GPU cost sitting around between generations. The tradeoff is
  that back-to-back requests almost always pay a full cold start (container
  boot + ComfyUI startup + model load onto the GPU, likely 30-90s+) instead
  of reusing a warm container.
- Every generated image is screened by `CompVis/stable-diffusion-safety-checker`
  before it's returned -- a flagged image is never written to disk or handed
  back to the caller; the caller just sees an error instead. The checker's
  weights are baked into the image at build time (no HF download at call
  time), so this only adds one CLIP classification pass per image, not a
  network round-trip.
