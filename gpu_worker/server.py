"""
gpu_worker.server -- lightweight NVENC encode worker.

Runs on a machine with an NVIDIA GPU (e.g. a laptop) and does nothing until
the NAS-hosted bot calls it. Idle cost is one listening socket -- no polling,
no persistent GPU/CUDA usage; ffmpeg (and the GPU) is only touched for the
duration of a single /encode request.

Endpoints:
    GET  /health   -- liveness check
    POST /encode   -- runs one ffmpeg job with GPU encoding, returns the
                      resulting file as raw bytes

The bot never sends a full ffmpeg command line -- it sends `args`, a JSON
list of ffmpeg arguments (everything after "ffmpeg -y") with placeholder
tokens "{input0}", "{input1}", ... and "{output}" standing in for local
paths. This server substitutes those tokens with paths inside its own temp
dir (after saving the uploaded `inputs` there) before running ffmpeg, so the
caller never needs to know anything about this machine's filesystem.

Config (env vars):
    GPU_WORKER_API_KEY  -- required. Requests must send a matching
                            X-Api-Key header.
    GPU_WORKER_PORT     -- defaults to 8802.

See README.md in this directory for auto-start (Task Scheduler) and
firewall setup -- this file only implements the listener itself.
"""

import json
import os
import shutil
import subprocess
import tempfile

from fastapi import FastAPI, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

API_KEY        = os.environ.get("GPU_WORKER_API_KEY")
PORT           = int(os.environ.get("GPU_WORKER_PORT", "8802"))
ENCODE_TIMEOUT = 300

app = FastAPI()


def _check_key(x_api_key: str | None) -> None:
    if not API_KEY or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="bad or missing API key")


@app.get("/health")
def health(x_api_key: str | None = Header(default=None)):
    _check_key(x_api_key)
    return {"status": "ok"}


@app.post("/encode")
async def encode(
    args: str = Form(...),
    output_suffix: str = Form(".mp4"),
    inputs: list[UploadFile] = [],
    x_api_key: str | None = Header(default=None),
):
    _check_key(x_api_key)

    try:
        arg_list = json.loads(args)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="args must be a JSON list")

    workdir = tempfile.mkdtemp(prefix="gpuworker_")
    try:
        substituted = list(arg_list)
        for i, upload in enumerate(inputs):
            ext = os.path.splitext(upload.filename or "")[1]
            in_path = os.path.join(workdir, f"input{i}{ext}")
            with open(in_path, "wb") as f:
                f.write(await upload.read())
            substituted = [a.replace(f"{{input{i}}}", in_path) for a in substituted]

        out_path = os.path.join(workdir, f"output{output_suffix}")
        substituted = [a.replace("{output}", out_path) for a in substituted]

        result = subprocess.run(
            ["ffmpeg", "-y"] + substituted,
            capture_output=True, timeout=ENCODE_TIMEOUT,
        )

        if result.returncode != 0 or not os.path.exists(out_path):
            stderr = result.stderr.decode("utf-8", errors="replace")[-2000:]
            print(f"[gpu_worker] encode failed: {stderr}", flush=True)
            raise HTTPException(status_code=500, detail="ffmpeg encode failed")

        print(f"[gpu_worker] encoded {out_path} ({os.path.getsize(out_path)} bytes)", flush=True)
        return FileResponse(
            out_path,
            media_type="application/octet-stream",
            # Cleanup runs after the response is fully streamed back, so the
            # file must still exist when this function returns.
            background=BackgroundTask(shutil.rmtree, workdir, ignore_errors=True),
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(status_code=504, detail="ffmpeg encode timed out")
    except HTTPException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    if not API_KEY:
        raise SystemExit("GPU_WORKER_API_KEY must be set")
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
