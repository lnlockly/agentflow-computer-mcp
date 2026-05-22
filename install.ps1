# AgentFlow Desktop installer (Windows).
#
# One-liner from the cabinet:
#   $env:AF_KEY="..."; $env:AF_DEVICE_ID="..."; $env:AF_DEVICE_TOKEN="..."
#   iwr -useb https://agentflow.website/install/computer-mcp.ps1 | iex
#
# Requirements: PowerShell 5.1+, Python 3.11+ on PATH.

$ErrorActionPreference = "Stop"

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
$AfDeviceId    = Read-IfMissing "AF_DEVICE_ID"    "Device ID (uuid from cabinet)"
$AfDeviceToken = Read-IfMissing "AF_DEVICE_TOKEN" "One-time device token"

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

# Write files without BOM — tomllib chokes on UTF-8 BOM with «Invalid
# statement (at line 1, column 1)», which broke v0.3.x daemons.
# PowerShell 5.1's `Set-Content -Encoding UTF8` adds a BOM by default;
# use [System.IO.File]::WriteAllText with explicit UTF8Encoding($false)
# which omits the BOM on all PS versions.
$Utf8NoBom = New-Object System.Text.UTF8Encoding $false

$AuthPath = Join-Path $AfDir "auth.json"
[System.IO.File]::WriteAllText($AuthPath, $Auth, $Utf8NoBom)
icacls $AuthPath /inheritance:r /grant:r "${env:USERNAME}:F" | Out-Null
Write-Host "[install] wrote $AuthPath"

$ScopePath = Join-Path $AfDir "computer-scope.toml"
if (-not (Test-Path $ScopePath)) {
    $ScopeBody = @"
allow_apps = []
allow_paths = []
deny_paths = ["%USERPROFILE%/.ssh", "%USERPROFILE%/AppData/Roaming/Microsoft/Crypto"]
shell_whitelist = []
confirm_before = ["computer.fs.write", "computer.shell.exec"]
max_actions_per_session = 50
budget_usd = 2.0
"@
    [System.IO.File]::WriteAllText($ScopePath, $ScopeBody, $Utf8NoBom)
    Write-Host "[install] wrote default scope at $ScopePath"
}

# Locate the entry-point. `site.USER_BASE` returns empty on Windows
# Store Python or with PYTHONNOUSERSITE set, which broke the install for
# at least one early adopter (NULL Path → Join-Path crash). Try each
# script dir Python knows about; if none has the .exe, fall back to
# `python -m` which works regardless of where pip dropped the launcher.
function Find-Scripts {
    $candidates = @()
    $userBase = & $PythonExe -c "import site; print(site.USER_BASE or '')" 2>$null
    if ($userBase) { $candidates += (Join-Path $userBase 'Scripts') }
    $userScripts = & $PythonExe -c "import sysconfig; print(sysconfig.get_path('scripts','nt_user') or '')" 2>$null
    if ($userScripts) { $candidates += $userScripts }
    $globalScripts = & $PythonExe -c "import sysconfig; print(sysconfig.get_path('scripts') or '')" 2>$null
    if ($globalScripts) { $candidates += $globalScripts }
    foreach ($dir in $candidates) {
        $exe = Join-Path $dir 'agentflow-desktop.exe'
        if (Test-Path $exe) { return $exe }
        $exe = Join-Path $dir 'agentflow-computer-mcp.exe'
        if (Test-Path $exe) { return $exe }
    }
    return $null
}

$Entrypoint = Find-Scripts
$TaskName = "AgentFlowDesktop"

if ($Entrypoint) {
    $Args = "run"
    if ($Entrypoint -like "*agentflow-computer-mcp*") { $Args = "--mode ws" }
    $Action = New-ScheduledTaskAction -Execute $Entrypoint -Argument $Args
} else {
    Write-Host "[install] launcher .exe not visible — falling back to 'python -m agentflow_computer_mcp.desktop_cli'"
    $Action = New-ScheduledTaskAction -Execute $PythonExe -Argument "-m agentflow_computer_mcp.desktop_cli run"
}
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
