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
if (-not $AfWsUrl) { $AfWsUrl = "wss://agentflow.website/_agents/_devices/connect" }

$AfDir = Join-Path $env:USERPROFILE ".agentflow"
New-Item -ItemType Directory -Force -Path $AfDir | Out-Null

# ---------------------------------------------------------------------------
# Detect Python — Microsoft Store installs may only expose `py -3`.
# ---------------------------------------------------------------------------
$PythonCmd = $null
foreach ($candidate in @("python", "py -3")) {
    try {
        $ver = & cmd /c "$candidate --version" 2>&1
        if ($LASTEXITCODE -eq 0 -and $ver -match "Python 3") {
            $PythonCmd = $candidate
            break
        }
    } catch { }
}

if (-not $PythonCmd) {
    Write-Host ""
    Write-Host "ERROR: Python 3 not found on PATH." -ForegroundColor Red
    Write-Host "Install Python from: https://www.python.org/downloads/windows/" -ForegroundColor Yellow
    Write-Host "  - Tick 'Add Python to PATH' during setup."
    Write-Host "  - Microsoft Store users: also try 'py -3 --version' from a fresh terminal."
    exit 1
}

Write-Host "Using Python: $PythonCmd"

if ($env:AF_PACKAGE_PATH) {
    & cmd /c "$PythonCmd -m pip install --user --upgrade $env:AF_PACKAGE_PATH"
} else {
    & cmd /c "$PythonCmd -m pip install --user --upgrade git+https://github.com/lnlockly/agentflow-computer-mcp.git"
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

# ---------------------------------------------------------------------------
# Find the entry-point binary pip --user dropped on disk.
# ---------------------------------------------------------------------------
$UserBase = & cmd /c "$PythonCmd -c `"import site; print(site.USER_BASE)`""
$Entrypoint = Join-Path $UserBase "Scripts\agentflow-desktop.exe"
if (-not (Test-Path $Entrypoint)) {
    $Entrypoint = Join-Path $UserBase "Scripts\agentflow-desktop"
}
# Fallback: old package name
if (-not (Test-Path $Entrypoint)) {
    $Entrypoint = Join-Path $UserBase "Scripts\agentflow-computer-mcp.exe"
}
if (-not (Test-Path $Entrypoint)) {
    $Entrypoint = Join-Path $UserBase "Scripts\agentflow-computer-mcp"
}
if (-not (Test-Path $Entrypoint)) {
    Write-Error "could not find agentflow-desktop entry point under $UserBase\Scripts"
    exit 1
}

# ---------------------------------------------------------------------------
# Task Scheduler — runs at logon AND at system startup.
# RunLevel = Limited (daemon does not need admin rights).
# RestartCount = 5 with 1-minute interval.
# ---------------------------------------------------------------------------
$TaskName = "AgentFlowDesktop"
$Action = New-ScheduledTaskAction -Execute $Entrypoint -Argument "run"

$TriggerLogon  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$TriggerBoot   = New-ScheduledTaskTrigger -AtStartup

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1)

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
Write-Host "Windows install complete."
Write-Host "  Task:      $TaskName (runs at logon + startup, restarts up to 5x on failure)"
Write-Host "  Logs:      %TEMP%\agentflow-desktop.log  (or Event Viewer → Task Scheduler)"
Write-Host "  Stop:      Stop-ScheduledTask -TaskName $TaskName"
Write-Host ""

# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------
$DesktopBin = Join-Path $UserBase "Scripts\agentflow-desktop.exe"
if (-not (Test-Path $DesktopBin)) {
    $DesktopBin = Join-Path $UserBase "Scripts\agentflow-desktop"
}
if (Test-Path $DesktopBin) {
    Write-Host "--- selftest ---"
    & $DesktopBin selftest
    if ($LASTEXITCODE -eq 0) {
        Write-Host "selftest: PASS"
    } else {
        Write-Host "selftest: FAIL (see above)"
    }
}
