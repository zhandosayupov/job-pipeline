# Registers the jobpipe browser worker to start at logon (hidden, restarted on failure).
# Run in PowerShell from the repository folder on the laptop:
#   powershell -ExecutionPolicy Bypass -File ops\worker\install-worker.ps1
# Before: uv sync --group worker ; uv run playwright install chromium
#         create %USERPROFILE%\.jobpipe-worker\worker.env with JOBPIPE_CORE_URL and WORKER_TOKEN
#         (the same token as WORKER_TOKEN in /etc/jobpipe/secrets.env on the VM)
#         log in once per platform: uv run jobpipe worker --login hh --login-url https://hh.kz/account/login
# Telegram userbot too (S5): add -Userbot. Before: uv sync --group userbot ; TG_API_ID and TG_API_HASH
#         (my.telegram.org, the secondary account) in worker.env ; uv run jobpipe userbot --login
param([switch]$Userbot)
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

if ($Userbot) {
    if (-not (Test-Path (Join-Path $wdir "userbot.session"))) {
        Write-Error "Log in first: uv run jobpipe userbot --login"
    }
    icacls (Join-Path $wdir "userbot.session") /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
    $ubAction = New-ScheduledTaskAction -Execute $uv -Argument "run --project `"$repo`" jobpipe userbot" `
        -WorkingDirectory $repo
    Register-ScheduledTask -TaskName "jobpipe-userbot" -Action $ubAction -Trigger $trigger -Settings $settings `
        -Description "jobpipe Telegram userbot (reads recruiter chats, saves drafts, never sends)" -Force | Out-Null
    Start-ScheduledTask -TaskName "jobpipe-userbot"
    Write-Host "jobpipe-userbot registered and started. Stop: Stop-ScheduledTask jobpipe-userbot"
}
