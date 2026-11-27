# Registers the jobpipe browser worker to start at logon (hidden, restarted on failure).
# Run in PowerShell from the repository folder on the laptop:
#   powershell -ExecutionPolicy Bypass -File ops\worker\install-worker.ps1
# Before: uv sync --group worker ; uv run playwright install chromium
#         create %USERPROFILE%\.jobpipe-worker\worker.env with JOBPIPE_CORE_URL and WORKER_TOKEN
#         (the same token as WORKER_TOKEN in /etc/jobpipe/secrets.env on the VM)
#         log in once per platform: uv run jobpipe worker --login hh --login-url https://hh.kz/account/login
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$wdir = Join-Path $env:USERPROFILE ".jobpipe-worker"
New-Item -ItemType Directory -Force -Path $wdir | Out-Null
if (-not (Test-Path (Join-Path $wdir "worker.env"))) {
    Write-Error "Create $wdir\worker.env first (JOBPIPE_CORE_URL=..., WORKER_TOKEN=...)"
}
# The token file is readable by the current user only.
icacls (Join-Path $wdir "worker.env") /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null

$uv = (Get-Command uv -ErrorAction Stop).Source
$action = New-ScheduledTaskAction -Execute $uv -Argument "run --project `"$repo`" jobpipe worker" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -Priority 7 -Hidden
Register-ScheduledTask -TaskName "jobpipe-worker" -Action $action -Trigger $trigger -Settings $settings `
    -Description "jobpipe browser worker (applies only on AC power when idle)" -Force | Out-Null
Start-ScheduledTask -TaskName "jobpipe-worker"
Write-Host "jobpipe-worker registered and started. Stop: Stop-ScheduledTask jobpipe-worker"
