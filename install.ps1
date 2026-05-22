# AgentFlow Desktop installer (Windows).
#
# One-liner from the cabinet:
#   $env:AGENTFLOW_API_KEY = '<key>'
#   iwr -useb https://agentflow.website/install/computer-mcp.ps1 | iex
#
# Legacy invocation (still supported):
#   $env:AF_KEY="..."; $env:AF_DEVICE_ID="..."; $env:AF_DEVICE_TOKEN="..."
#   iwr -useb https://agentflow.website/install/computer-mcp.ps1 | iex
#
# AGENTFLOW_API_KEY is the canonical env var; AF_KEY stays as alias.
# Device id / token are optional now — daemon enrols on first launch.
#
# Requirements: PowerShell 5.1+, Python 3.11+ on PATH.

$ErrorActionPreference = "Stop"

# Bridge AGENTFLOW_API_KEY → AF_KEY so the rest of the script keeps the
# existing semantics. AGENTFLOW_API_KEY wins when both are set.
if ($env:AGENTFLOW_API_KEY -and -not $env:AF_KEY) {
    $env:AF_KEY = $env:AGENTFLOW_API_KEY
}

function Read-IfMissing {
    param([string]$Name, [string]$Prompt)
    $value = (Get-Item -Path "Env:$Name" -ErrorAction SilentlyContinue).Value
    if (-not $value) {
        if ($Host.UI.RawUI) {
            $value = Read-Host -Prompt $Prompt
        }
        if (-not $value) {
            Write-Error "missing required env var: $Name"
            exit 1
        }
        Set-Item -Path "Env:$Name" -Value $value
    }
    return $value
}

$AfKey         = Read-IfMissing "AF_KEY"          "AgentFlow API key (af_live_...)"
$AfDeviceId    = $env:AF_DEVICE_ID
$AfDeviceToken = $env:AF_DEVICE_TOKEN

$AfWsUrl = $env:AF_WS_URL
if (-not $AfWsUrl) { $AfWsUrl = "wss://agentflow.website/_agents/_devices/connect" }

# Resolve Python.
$Python = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $Python) { $Python = (Get-Command python3 -ErrorAction SilentlyContinue) }
if (-not $Python) {
    Write-Error "python not found on PATH; install Python 3.11+ from https://python.org first"
    exit 1
}
$PythonExe = $Python.Source

$AfDir = Join-Path $env:USERPROFILE ".agentflow"
New-Item -ItemType Directory -Force -Path $AfDir | Out-Null

# pip install (--user respects current Python).
if ($env:AF_PACKAGE_PATH) {
    Write-Host "[install] installing package from $($env:AF_PACKAGE_PATH)"
    & $PythonExe -m pip install --user --upgrade $env:AF_PACKAGE_PATH
} else {
    Write-Host "[install] installing agentflow-computer-mcp from GitHub"
    & $PythonExe -m pip install --user --upgrade "git+https://github.com/lnlockly/agentflow-computer-mcp.git"
}

# Auth file with restrictive ACL.
$Auth = [PSCustomObject]@{
    api_key          = $AfKey
    device_id        = $AfDeviceId
    enrollment_token = $AfDeviceToken
    device_secret    = ""
    ws_url           = $AfWsUrl
} | ConvertTo-Json -Depth 3

$AuthPath = Join-Path $AfDir "auth.json"
Set-Content -Path $AuthPath -Value $Auth -Encoding UTF8
icacls $AuthPath /inheritance:r /grant:r "${env:USERNAME}:F" | Out-Null
Write-Host "[install] wrote $AuthPath"

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
    Write-Host "[install] wrote default scope at $ScopePath"
}

# Find the entry-point script that pip --user dropped.
$UserBase = & $PythonExe -c "import site; print(site.USER_BASE)"
$Entrypoint = Join-Path $UserBase "Scripts\agentflow-desktop.exe"
if (-not (Test-Path $Entrypoint)) {
    $Entrypoint = Join-Path $UserBase "Scripts\agentflow-computer-mcp.exe"
}
if (-not (Test-Path $Entrypoint)) {
    Write-Error "could not find AgentFlow Desktop entry point under $UserBase\Scripts"
    exit 1
}

$TaskName = "AgentFlowDesktop"
$Args = "run"
if ($Entrypoint -like "*agentflow-computer-mcp*") { $Args = "--mode ws" }

$Action = New-ScheduledTaskAction -Execute $Entrypoint -Argument $Args
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
Write-Host "AgentFlow Desktop installed."
Write-Host "  Cabinet: https://agentflow.website/cabinet/devices/$AfDeviceId/live"
Write-Host "  Task:    $TaskName (runs at logon)"
Write-Host "  Verify:  agentflow-desktop selftest"
Write-Host "  Stop:    Stop-ScheduledTask -TaskName $TaskName"
