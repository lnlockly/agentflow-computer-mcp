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

# Resolve Python — Microsoft Store installs sometimes only expose `py` or `py -3`.
$PythonExe = $null
foreach ($candidate in @("python", "py", "python3")) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) {
        # Verify it's actually Python 3 (py.exe without args may launch 2.7).
        $ver = & $cmd.Source "--version" 2>&1
        if ($ver -match "Python 3") {
            $PythonExe = $cmd.Source
            break
        }
        # Try `py -3` for the Windows py launcher.
        if ($candidate -eq "py") {
            $ver3 = & $cmd.Source "-3" "--version" 2>&1
            if ($ver3 -match "Python 3") {
                # Wrap as a script-level alias — we pass -3 explicitly going forward.
                $PythonExe = "$($cmd.Source) -3"
                break
            }
        }
    }
}
if (-not $PythonExe) {
    Write-Host ""
    Write-Host "ERROR: Python 3 not found on PATH." -ForegroundColor Red
    Write-Host "Install from: https://www.python.org/downloads/windows/" -ForegroundColor Yellow
    Write-Host "  - Tick 'Add Python to PATH' during setup."
    Write-Host "  - After installing from Microsoft Store, open a new terminal and retry."
    exit 1
}
Write-Host "[install] using Python: $PythonExe"

$AfDir = Join-Path $env:USERPROFILE ".agentflow"
New-Item -ItemType Directory -Force -Path $AfDir | Out-Null

# pip install (--user respects current Python).
# PythonExe may be "C:\path\py.exe -3" — split into exe+args for Invoke-Expression.
if ($env:AF_PACKAGE_PATH) {
    Write-Host "[install] installing package from $($env:AF_PACKAGE_PATH)"
    Invoke-Expression "$PythonExe -m pip install --user --upgrade `"$($env:AF_PACKAGE_PATH)`""
} else {
    Write-Host "[install] installing agentflow-computer-mcp from GitHub"
    Invoke-Expression "$PythonExe -m pip install --user --upgrade `"git+https://github.com/lnlockly/agentflow-computer-mcp.git`""
}

# Resolve user-base for finding pip-installed scripts.
$UserBase = Invoke-Expression "$PythonExe -c `"import site; print(site.USER_BASE)`""

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

# Two triggers: one for user logon, one at system startup (handles cold-boot cases
# where the user session may not be interactive immediately).
$TriggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$TriggerBoot  = New-ScheduledTaskTrigger -AtStartup

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -RestartCount 5

# RunLevel Limited — daemon does not need administrator rights.
$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger @($TriggerLogon, $TriggerBoot) `
    -Settings $Settings `
    -Principal $Principal `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "AgentFlow Desktop installed."
Write-Host "  Cabinet: https://agentflow.website/cabinet/devices/$AfDeviceId/live"
Write-Host "  Task:    $TaskName (runs at logon + system startup; restarts up to 5x on failure)"
Write-Host "  Verify:  agentflow-desktop selftest"
Write-Host "  Stop:    Stop-ScheduledTask -TaskName $TaskName"

# Selftest
$SelftestBin = Join-Path $UserBase "Scripts\agentflow-desktop.exe"
if (-not (Test-Path $SelftestBin)) {
    $SelftestBin = Join-Path $UserBase "Scripts\agentflow-desktop"
}
if (Test-Path $SelftestBin) {
    Write-Host ""
    Write-Host "[install] running selftest..."
    & $SelftestBin selftest
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[install] selftest: PASS"
    } else {
        Write-Host "[install] selftest: FAIL (see above)"
    }
}
