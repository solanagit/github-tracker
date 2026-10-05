<#
Sets up GitHub Tracker on this Windows machine. Run it from the cloned folder in an
Administrator PowerShell:

    powershell -ExecutionPolicy Bypass -File .\install.ps1

It asks for your Discord webhook URL (saved only in .env on this machine, never on GitHub),
then creates a "GitHub Tracker" scheduled task that runs every 10 minutes, even when nobody
is logged in. Each run first pulls the latest version of this folder from GitHub.

Safe to run again, e.g. after moving the folder or reinstalling Python.
To remove:  Unregister-ScheduledTask -TaskName 'GitHub Tracker' -Confirm:$false
#>
#Requires -RunAsAdministrator
$ErrorActionPreference = 'Stop'
$dir = $PSScriptRoot
$taskName = 'GitHub Tracker'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Fail($msg) { Write-Host $msg -ForegroundColor Red; exit 1 }

# 1. Python and Git
$python = $null
foreach ($cmd in 'python', 'py') {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { continue }
    try { $found = & $cmd -c "import sys; print(sys.executable)" 2>$null } catch { $found = $null }
    if ($found) { $python = ($found | Select-Object -Last 1).Trim(); break }
}
if (-not $python) {
    Fail 'Python not found. Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then open a new PowerShell and run this again.'
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Fail 'Git not found. Install it from https://git-scm.com/download/win, then open a new PowerShell and run this again.'
}
if (-not (Test-Path (Join-Path $dir '.git'))) {
    Fail 'This folder needs to come from "git clone" so it can auto-update. See README.md.'
}
Write-Host "Python: $python"

# 2. Discord webhook, kept in .env (which .gitignore keeps off GitHub)
$envFile = Join-Path $dir '.env'
if ((Test-Path $envFile) -and (Select-String -Path $envFile -Pattern '^DISCORD_WEBHOOK_URL=https' -Quiet)) {
    Write-Host 'Using the Discord webhook already saved in .env'
} else {
    while ($true) {
        $url = (Read-Host 'Paste your Discord webhook URL').Trim()
        if ($url -notmatch '^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$') {
            Write-Host 'That is not a Discord webhook link. It should start with https://discord.com/api/webhooks/' -ForegroundColor Yellow
            continue
        }
        try { $hook = Invoke-RestMethod -Uri $url -UseBasicParsing; break }  # GET only reads the webhook's details
        catch { Write-Host 'Discord does not recognise that webhook. Copy it again and paste it.' -ForegroundColor Yellow }
    }
    Set-Content -Path $envFile -Value "DISCORD_WEBHOOK_URL=$url`r`nGITHUB_TOKEN=" -Encoding Ascii
    Write-Host "Saved. Messages will be posted as webhook '$($hook.name)'."
}
# Only SYSTEM and Administrators may read the webhook URL.
icacls $envFile /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null

# 3. Scheduled task: every 10 minutes, as SYSTEM so it keeps running when you log off or the VPS restarts
$action = New-ScheduledTaskAction -Execute $python -Argument "`"$dir\tracker.py`" --update" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(10) -RepetitionInterval (New-TimeSpan -Minutes 10)
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description "Posts new GitHub commits to Discord. Folder: $dir" -Force | Out-Null
Write-Host "Scheduled task '$taskName' created."

# 4. First check right now
Write-Host 'Running the first check...'
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 3
$deadline = (Get-Date).AddSeconds(90)
while ((Get-ScheduledTask -TaskName $taskName).State -eq 'Running' -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 2 }

$log = Join-Path $dir 'tracker.log'
if (Test-Path $log) { Get-Content $log -Tail 5 }
if ((Get-ScheduledTaskInfo -TaskName $taskName).LastTaskResult -eq 0) {
    Write-Host "`nAll set. Check your Discord channel for a 'Now tracking' message." -ForegroundColor Green
} else {
    Write-Host "`nThe first check reported a problem. Details are in tracker.log (shown above)." -ForegroundColor Red
}
