<#
Sets up the GitHub Tracker bot on this Windows machine. Run it from the cloned folder in an
Administrator PowerShell:

    powershell -ExecutionPolicy Bypass -File .\install.ps1

It asks for your Discord bot token (saved only in .env on this machine, never on GitHub) and,
optionally, a GitHub token. It installs what the bot needs and creates a "GitHub Tracker"
scheduled task that keeps the bot running, including after the VPS restarts. The bot then
updates itself from GitHub.

Safe to run again, for example to add or replace the GitHub token. To switch bots, delete .env first.
To remove:  Stop-ScheduledTask -TaskName 'GitHub Tracker'; Unregister-ScheduledTask -TaskName 'GitHub Tracker' -Confirm:$false
#>
#Requires -RunAsAdministrator
$ErrorActionPreference = 'Stop'
$dir = $PSScriptRoot
$taskName = 'GitHub Tracker'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Fail($msg) { Write-Host $msg -ForegroundColor Red; exit 1 }
function Read-Secret($prompt) {
    $secure = Read-Host $prompt -AsSecureString
    return [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)).Trim()
}

# 1. Python 3.11+ and Git
$python = $null
foreach ($cmd in 'python', 'py') {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { continue }
    try { $found = & $cmd -c "import sys; print(sys.executable if sys.version_info >= (3, 11) else '')" 2>$null } catch { $found = $null }
    if ($found) { $python = ([string]($found | Select-Object -Last 1)).Trim() }
    if ($python) { break }
}
if (-not $python) {
    Fail ('Python 3.11 or newer not found. Go to https://www.python.org/downloads/windows/, download the ' +
          '"Windows installer (64-bit)" of the latest Python 3, tick "Add python.exe to PATH", click Install Now, ' +
          'then open a new PowerShell and run this again.')
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Fail 'Git not found. Install it from https://git-scm.com/download/win, then open a new PowerShell and run this again.'
}
if (-not (Test-Path (Join-Path $dir '.git'))) {
    Fail 'This folder needs to come from "git clone" so it can update itself. See README.md.'
}

# 2. Lock the folder down: SYSTEM runs this code, so only SYSTEM and Administrators may change it.
$envFile = Join-Path $dir '.env'
function Lock-EnvFile {  # only SYSTEM (which runs the bot) and Administrators may read the tokens
    if (Test-Path $envFile) { icacls $envFile /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null }
}
icacls $dir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' /C /Q | Out-Null
icacls (Join-Path $dir '*') /reset /T /C /Q | Out-Null
Lock-EnvFile  # the reset above opened it up again

# 3. A private Python environment with what the bot needs
$venvPython = Join-Path $dir '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    Write-Host 'Creating a Python environment for the bot...'
    & $python -m venv (Join-Path $dir '.venv')
    if ($LASTEXITCODE -ne 0) { Fail 'Could not create the Python environment.' }
}
Write-Host 'Installing what the bot needs (takes a minute the first time)...'
& $venvPython -m pip install --quiet --disable-pip-version-check -r (Join-Path $dir 'requirements.txt')
if ($LASTEXITCODE -ne 0) { Fail 'Installing requirements failed (see the messages above).' }

# 4. Tokens, kept in .env (which .gitignore keeps off GitHub)
function Read-EnvValue($name) {
    if (-not (Test-Path $envFile)) { return '' }
    $m = Select-String -Path $envFile -Pattern "^$name=(.*)$" | Select-Object -First 1
    if ($m) { return $m.Matches[0].Groups[1].Value.Trim() } else { return '' }
}
function Get-DiscordApp($t) {
    # Returns the bot's application, $null if Discord rejects the token, and stops on network trouble.
    try {
        # Discord blocks PowerShell's default browser-like User-Agent, so send a bot-style one.
        return Invoke-RestMethod -Uri 'https://discord.com/api/v10/applications/@me' -Headers @{ Authorization = "Bot $t" } `
            -UserAgent 'DiscordBot (https://github.com, 2.0)' -UseBasicParsing
    } catch {
        $code = 0
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
        if ($code -eq 401) { return $null }
        Fail "Couldn't reach Discord ($($_.Exception.Message)). Check the VPS's internet connection, then run this again."
    }
}
$token = Read-EnvValue 'DISCORD_TOKEN'
$app = $null
if ($token) {
    $app = Get-DiscordApp $token
    if ($app) { Write-Host "Using the bot token already saved in .env ($($app.name))." }
    else { Write-Host 'The bot token saved in .env no longer works.' -ForegroundColor Yellow }
}
while (-not $app) {
    $token = Read-Secret 'Paste your Discord bot token (it stays hidden while you paste, then press Enter)'
    if ($token) { $app = Get-DiscordApp $token }
    if (-not $app) {
        Write-Host 'Discord did not accept that token. In the Developer Portal, open your app > Bot > Reset Token, and copy the new one.' -ForegroundColor Yellow
    }
}
function Test-GitHubToken($t) {
    # True if GitHub accepts the token. Asking for the rate limit doesn't use any of it.
    try {
        Invoke-RestMethod -Uri 'https://api.github.com/rate_limit' -Headers @{ Authorization = "Bearer $t" } `
            -UserAgent 'github-tracker/2.0' -UseBasicParsing | Out-Null
        return $true
    } catch {
        $code = 0
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
        if ($code -eq 401) { return $false }
        Fail "Couldn't reach GitHub ($($_.Exception.Message)). Check the VPS's internet connection, then run this again."
    }
}
$githubToken = Read-EnvValue 'GITHUB_TOKEN'
$have = if ($githubToken) { 'one is saved: Enter keeps it, "none" removes it' } else { 'Enter to skip' }
while ($true) {
    $answer = Read-Secret "Optional: paste a GitHub token to track more than about 7 repos ($have)"
    if (-not $answer) { break }
    if ($answer -eq 'none') { $githubToken = ''; break }
    if (Test-GitHubToken $answer) { $githubToken = $answer; break }
    Write-Host 'GitHub did not accept that token (expired, or copied wrong?). Try again, or press Enter to skip.' -ForegroundColor Yellow
}
Set-Content -Path $envFile -Value "DISCORD_TOKEN=$token`r`nGITHUB_TOKEN=$githubToken" -Encoding Ascii
Lock-EnvFile

# 5. Scheduled task: starts the bot at boot, and every minute restarts it if it isn't running.
#    It runs as SYSTEM so it keeps going when you log off the VPS.
$action = New-ScheduledTaskAction -Execute $venvPython -Argument "`"$(Join-Path $dir 'run.py')`"" -WorkingDirectory $dir
$triggers = @(
    (New-ScheduledTaskTrigger -AtStartup),
    (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1))
)
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $taskName  # stop the old copy so the new settings and tokens take over
}
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Principal $principal `
    -Settings $settings -Description "Discord bot that posts new GitHub commits. Folder: $dir" -Force | Out-Null

# 6. Start it and wait for it to come online
$status = Join-Path $dir 'data\status.txt'
Remove-Item $status -ErrorAction SilentlyContinue
Write-Host 'Starting the bot...'
Start-ScheduledTask -TaskName $taskName
$deadline = (Get-Date).AddSeconds(120)
while (-not (Test-Path $status) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 3 }

$invite = "https://discord.com/oauth2/authorize?client_id=$($app.id)&scope=bot%20applications.commands&permissions=19456"
if (Test-Path $status) {
    Write-Host "`nThe bot is online as $($app.name)." -ForegroundColor Green
} else {
    Write-Host "`nThe bot hasn't come online yet. The last messages in its log:" -ForegroundColor Red
    $log = Join-Path $dir 'logs\bot.log'
    if (Test-Path $log) {  # just the messages, not the technical detail under them
        Get-Content $log -Encoding UTF8 | Select-String '^\d{4}-\d\d-\d\d ' | Select-Object -Last 8 | ForEach-Object { $_.Line }
    }
}
Write-Host "`nLast step: add the bot to your Discord server. Open this link (on any computer):"
Write-Host $invite -ForegroundColor Cyan
try { Set-Clipboard -Value $invite; Write-Host '(It is also copied to the clipboard.)' } catch { }
Write-Host "`nThen, in your server, type /add followed by a GitHub link."
