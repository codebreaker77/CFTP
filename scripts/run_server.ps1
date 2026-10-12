# PowerShell script to launch CFTP server
param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 9000,
    [string]$StorageDir = "server_storage",
    [string]$LogLevel = "INFO"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  Starting CFTP/1.0 Server on ${HostAddress}:$Port" -ForegroundColor Cyan
Write-Host "  Storage Directory: $StorageDir" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

Set-Location -Path $ProjectRoot
$env:PYTHONPATH = $ProjectRoot
python -m cftp.server --host $HostAddress --port $Port --storage-dir $StorageDir --log-level $LogLevel
