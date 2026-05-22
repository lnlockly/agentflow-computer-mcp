# AgentFlow Desktop installer (Windows).
# Requires: PowerShell 5.1+, Python 3.11+ on PATH, AF_KEY + AF_DEVICE_TOKEN + AF_DEVICE_ID env vars.

$ErrorActionPreference = "Stop"

foreach ($var in @("AF_KEY", "AF_DEVICE_TOKEN", "AF_DEVICE_ID")) {
    if (-not (Get-Item -Path "Env:$var" -ErrorAction SilentlyContinue)) {
        Write-Error "missing required env var: $var"
        exit 1
    }
}

$AfWsUrl = $env:AF_WS_URL
if (-not $AfWsUrl) { $AfWsUrl = "wss://agentflow.website/_devices/connect" }

$AfDir = Join-Path $env:USERPROFILE ".agentflow"
New-Item -ItemType Directory -Force -Path $AfDir | Out-Null

if ($env:AF_PACKAGE_PATH) {
    & python -m pip install --user --upgrade $env:AF_PACKAGE_PATH
} else {
    & python -m pip install --user --upgrade "git+https://github.com/lnlockly/agentflow-computer-mcp.git"
}

$Auth = @{
    api_key          = $env:AF_KEY
    device_id        = $env:AF_DEVICE_ID
    enrollment_token = $env:AF_DEVICE_TOKEN
    device_secret    = ""
    ws_url           = $AfWsUrl
} | ConvertTo-Json -Depth 3

$AuthPath = Join-Path $AfDir "auth.json"
Set-Content -Path $AuthPath -Value $Auth -Encoding UTF8
# Restrict to current user only.
icacls $AuthPath /inheritance:r /grant:r "${env:USERNAME}:F" | Out-Null

$ScopePath = Join-Path $AfDir "computer-scope.toml"
if (-not (Test-Path $ScopePath)) {
    @"
allow_apps = []
allow_paths = []
deny_paths = ["%USERPROFILE%/.ssh", "%USERPROFILE%/AppData/Roaming/Microsoft/Crypto"]
shell_whitelist = []
confirm_before = ["computer.fs.write", "computer.shell.exec"]
max_actions_per_session = 50
budget_usd = 2.0
"@ | Set-Content -Path $ScopePath -Encoding UTF8
}

# Find the entry-point script pip --user dropped on disk.
$UserScripts = & python -c "import site; print(site.USER_BASE)"
$Entrypoint = Join-Path $UserScripts "Scripts\agentflow-computer-mcp.exe"
if (-not (Test-Path $Entrypoint)) {
    $Entrypoint = Join-Path $UserScripts "Scripts\agentflow-computer-mcp"
}
if (-not (Test-Path $Entrypoint)) {
    Write-Error "could not find agentflow-computer-mcp entry point under $UserScripts\Scripts"
    exit 1
}

# Task Scheduler entry that runs on user logon.
$TaskName = "AgentFlowComputerMcp"
$Action = New-ScheduledTaskAction -Execute $Entrypoint -Argument "--mode ws"
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -RestartCount 3
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "Windows install complete."
Write-Host "  Task: $TaskName (runs at logon)"
Write-Host "  Verify: agentflow-desktop selftest"
Write-Host "  Stop:   Stop-ScheduledTask -TaskName $TaskName"
