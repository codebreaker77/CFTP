param(
    [int]$Port = 8787,
    [int]$CftpPort = 9000,
    [switch]$ExistingServer,
    [switch]$Background
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
$webArguments = @('-m', 'cftp.web', '--port', "$Port", '--cftp-port', "$CftpPort")
if (-not $ExistingServer) { $webArguments += '--demo' }
if ($Background) {
    $runtimeDirectory = Join-Path (Get-Location) '.web-run'
    New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null
    $pidFile = Join-Path $runtimeDirectory 'server.pid'
    if (Test-Path -LiteralPath $pidFile) {
        $recordedProcess = Get-Process -Id ([int](Get-Content -LiteralPath $pidFile -Raw).Trim()) -ErrorAction SilentlyContinue
        if ($recordedProcess) { throw 'A background workspace is already recorded. Stop it with .\scripts\stop_web.ps1 first.' }
    }
    $pythonPath = (Get-Command python -ErrorAction Stop).Source
    $webProcess = Start-Process -FilePath $pythonPath -ArgumentList $webArguments -WorkingDirectory (Get-Location).Path -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimeDirectory 'server.log') -RedirectStandardError (Join-Path $runtimeDirectory 'server-error.log')
    Set-Content -LiteralPath $pidFile -Value $webProcess.Id
    Start-Sleep -Milliseconds 700
    if ($webProcess.HasExited) { throw "The workspace could not start. Check $runtimeDirectory\server-error.log." }
    Write-Host "Workspace running at http://127.0.0.1:${Port}"
    Write-Host 'Stop it with .\scripts\stop_web.ps1'
} else {
    python @webArguments
}
