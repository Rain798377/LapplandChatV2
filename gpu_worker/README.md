# gpu_worker

A tiny NVENC encode listener for the laptop's GPU. It sits idle (no CPU/GPU
use, ~0 overhead) until the NAS-hosted bot calls `/encode`. Not a Python
package in the `app/` sense -- this runs on a different machine entirely.

## Setup (Windows)

1. Install deps: `pip install -r gpu_worker/requirements.txt`
2. Pick an API key (any random string) and a port (default `8802`).
3. Confirm `ffmpeg` on PATH has NVENC: `ffmpeg -hide_banner -encoders | findstr nvenc`
4. Allow the port through Windows Firewall (adjust the port if you changed it):
   ```powershell
   New-NetFirewallRule -DisplayName "gpu_worker" -Direction Inbound -Protocol TCP -LocalPort 8802 -Action Allow
   ```
5. Set power options so the machine doesn't sleep while plugged in (Settings
   > System > Power > Screen and sleep), otherwise the NAS just can't reach it.
6. Register it to auto-start via Task Scheduler so it's always listening
   without logging in and starting it by hand:
   ```powershell
   $action  = New-ScheduledTaskAction -Execute "python.exe" -Argument "D:\LapplandChatV2\gpu_worker\server.py" -WorkingDirectory "D:\LapplandChatV2\gpu_worker"
   $trigger = New-ScheduledTaskTrigger -AtStartup
   $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Highest
   Register-ScheduledTask -TaskName "gpu_worker" -Action $action -Trigger $trigger -Principal $principal
   ```
   `GPU_WORKER_API_KEY` needs to be set as a **system** (not user) environment
   variable so it's visible to the task when nobody's logged in:
   ```powershell
   [System.Environment]::SetEnvironmentVariable("GPU_WORKER_API_KEY", "<your-key>", "Machine")
   ```
7. On the NAS side, set `GPU_WORKER_URL=http://<laptop-lan-ip>:8802` and
   `GPU_WORKER_API_KEY=<same-key>` in `.env`.

## Behavior

- `GET /health` -- returns `{"status": "ok"}` if the API key matches.
- `POST /encode` -- multipart form: `args` (JSON list of ffmpeg args with
  `{input0}`, `{input1}`, ... and `{output}` placeholders), `output_suffix`,
  and file(s) under `inputs`. Runs `ffmpeg -y <substituted args>` and streams
  the resulting file back. Non-zero exit or a missing output file returns a
  5xx so the caller falls back to local CPU encoding.
- If the laptop is off or unreachable, the NAS side just times out fast
  (`GPU_WORKER_CONNECT_TIMEOUT` in `app/core/config.py`) and encodes locally
  on CPU instead -- nothing on the NAS side depends on this worker existing.
