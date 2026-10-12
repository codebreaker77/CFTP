# PowerShell script to launch CFTP client
param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 9000,
    [string]$DownloadDir = "downloads"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir

Write-Host "==========================================================" -ForegroundColor Green
Write-Host "  Launching CFTP/1.0 Client connecting to ${HostAddress}:$Port" -ForegroundColor Green
Write-Host "  Download Directory: $DownloadDir" -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Green

Set-Location -Path $ProjectRoot
$env:PYTHONPATH = $ProjectRoot
python -m cftp.client --host $HostAddress --port $Port --download-dir $DownloadDir
