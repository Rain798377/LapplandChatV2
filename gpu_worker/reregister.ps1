# Re-registers the gpu_worker firewall rule, scheduled task, and Machine-scope
# API key on this machine (see README.md), then does one debug-logged run so
# failures are visible instead of silently showing LastTaskResult = 1.
#
# Self-elevates via UAC if not already running as Administrator -- just run
# this normally (double-click "Run with PowerShell", or `.\reregister.ps1`).

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}

$ErrorActionPreference = "Stop"

# The Task Scheduler operational log is off by default, so a task that fails
# before its process even launches (e.g. an S4U logon failure) leaves no
# trace anywhere. Turn it on so we can see the real error below.
try { wevtutil sl Microsoft-Windows-TaskScheduler/Operational /e:true } catch {}

$repoRoot  = Split-Path -Parent $PSCommandPath
$logPath   = Join-Path $repoRoot "task_debug.log"
# Use the project venv's python.exe explicitly -- a bare "python.exe" resolves
# differently under the task's non-interactive S4U logon (no venv activation)
# than in an interactive shell, landing on a global install that's missing
# gpu_worker's deps even when they're installed and importable right here.
$venvPython = Join-Path (Split-Path -Parent $repoRoot) ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Warning "Expected venv python at $venvPython but it doesn't exist -- falling back to bare python.exe (may hit PATH resolution issues)."
    $venvPython = "python.exe"
}

# ── 1. Firewall rule ─────────────────────────────────────────────────────
if (-not (Get-NetFirewallRule -DisplayName "gpu_worker" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "gpu_worker" -Direction Inbound -Protocol TCP -LocalPort 8802 -Action Allow | Out-Null
    Write-Host "Firewall rule created."
} else {
    Write-Host "Firewall rule already present."
}

# ── 2. Machine-scope API key ─────────────────────────────────────────────
$existing = [System.Environment]::GetEnvironmentVariable("GPU_WORKER_API_KEY", "Machine")
if (-not $existing) {
    $userVal = [System.Environment]::GetEnvironmentVariable("GPU_WORKER_API_KEY", "User")
    if ($userVal) {
        [System.Environment]::SetEnvironmentVariable("GPU_WORKER_API_KEY", $userVal, "Machine")
        Write-Host "Machine-scope API key set (copied from existing User-scope value)."
    } else {
        Write-Warning "No GPU_WORKER_API_KEY found at User or Machine scope. Set one before continuing:"
        Write-Warning '  [System.Environment]::SetEnvironmentVariable("GPU_WORKER_API_KEY", "<key>", "Machine")'
        Read-Host "Press Enter to exit"
        exit 1
    }
} else {
    Write-Host "Machine-scope API key already present."
}

# ── 3. Free up port 8802 if some earlier process is still holding it ────
$holder = Get-NetTCPConnection -LocalPort 8802 -State Listen -ErrorAction SilentlyContinue
if ($holder) {
    $holder | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {
        Write-Host "Stopping process $_ currently listening on 8802..."
        Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
}

# ── 4. Re-register the scheduled task from scratch ───────────────────────
Unregister-ScheduledTask -TaskName "gpu_worker" -Confirm:$false -ErrorAction SilentlyContinue

$cleanAction = New-ScheduledTaskAction -Execute $venvPython -Argument "$repoRoot\server.py" -WorkingDirectory $repoRoot
$trigger     = New-ScheduledTaskTrigger -AtStartup
$principal   = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Highest
Register-ScheduledTask -TaskName "gpu_worker" -Action $cleanAction -Trigger $trigger -Principal $principal | Out-Null
Write-Host "Scheduled task re-registered."

# ── 5. One debug-logged run to confirm it actually starts ───────────────
if (Test-Path $logPath) { Remove-Item $logPath -Force }
# PowerShell instead of cmd.exe for the debug wrapper -- cmd's `>` redirection
# combined with quoted paths in an Argument string is unreliable here (it ran
# and exited but never actually wrote the log). `*>` captures all streams,
# including uncaught exceptions, reliably.
$debugCommand = "& '$venvPython' server.py *> '$logPath'"
$debugAction  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -Command `"$debugCommand`"" -WorkingDirectory $repoRoot
Set-ScheduledTask -TaskName "gpu_worker" -Action $debugAction | Out-Null
Start-ScheduledTask -TaskName "gpu_worker"
Start-Sleep -Seconds 3

Write-Host "`n--- task_debug.log ---"
if (Test-Path $logPath) {
    Get-Content $logPath
} else {
    Write-Host "(no output -- process likely never launched; checking Task Scheduler's own event log below)"
}

Write-Host "`n--- Task Scheduler operational log (gpu_worker) ---"
try {
    Get-WinEvent -LogName 'Microsoft-Windows-TaskScheduler/Operational' -MaxEvents 50 -ErrorAction Stop |
        Where-Object { $_.Message -match 'gpu_worker' } |
        Select-Object -First 10 TimeCreated, Id, LevelDisplayName, Message |
        Format-List
} catch {
    Write-Host "(no matching events: $($_.Exception.Message))"
}

# ── 6. Restore the clean (non-debug) action for future boots ────────────
Set-ScheduledTask -TaskName "gpu_worker" -Action $cleanAction | Out-Null
Write-Host "`nDone. Task action restored to normal form for future startups."
Read-Host "Press Enter to close"
