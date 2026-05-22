# Remove scheduled task + auth files. Leaves the pip package installed.
$ErrorActionPreference = "Stop"

$TaskName = "AgentFlowComputerMcp"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "removed scheduled task: $TaskName"
}

$AuthPath = Join-Path $env:USERPROFILE ".agentflow\auth.json"
if (Test-Path $AuthPath) {
    Remove-Item $AuthPath
    Write-Host "removed $AuthPath"
}

Write-Host "uninstall complete (pip package retained — remove with: python -m pip uninstall agentflow-computer-mcp)"
