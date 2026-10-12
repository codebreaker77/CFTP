$ErrorActionPreference = 'Stop'
$pidFile = Join-Path (Split-Path -Parent $PSScriptRoot) '.web-run/server.pid'
if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Host 'No background workspace is recorded.'
    exit 0
}
$webProcessId = [int](Get-Content -LiteralPath $pidFile -Raw).Trim()
$webProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $webProcessId"
if ($webProcess) {
    if ($webProcess.CommandLine -notmatch '-m\s+cftp\.web\b') {
        throw 'The recorded process is no longer a CFTP web workspace; it was left running.'
    }
    Stop-Process -Id $webProcessId
    Write-Host 'CFTP web workspace stopped.'
} else {
    Write-Host 'The workspace has already stopped.'
}
Remove-Item -LiteralPath $pidFile
