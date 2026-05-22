@echo off
REM AgentFlow Desktop installer (Windows cmd.exe wrapper).
REM Pipes the canonical install.ps1 into PowerShell with execution policy bypass.
REM
REM Usage from cmd.exe:
REM   set AF_KEY=...
REM   set AF_DEVICE_ID=...
REM   set AF_DEVICE_TOKEN=...
REM   curl -sSL https://agentflow.website/install/computer-mcp.bat -o install.bat ^&^& install.bat
REM
REM Or one-line:
REM   powershell -ExecutionPolicy Bypass -Command "iwr -useb https://agentflow.website/install/computer-mcp.ps1 | iex"

powershell -ExecutionPolicy Bypass -NoProfile -Command "& {iwr -useb https://agentflow.website/install/computer-mcp.ps1 | iex}"
