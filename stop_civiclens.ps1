$ErrorActionPreference = "Stop"
$pidPath = Join-Path $PSScriptRoot ".civiclens.pid"

if (-not (Test-Path -LiteralPath $pidPath)) {
    Write-Host "No CIVICLENS launcher process was recorded." -ForegroundColor DarkGray
    exit 0
}

$serverId = [int](Get-Content -LiteralPath $pidPath -Raw).Trim()
$processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $serverId" -ErrorAction SilentlyContinue
if ($processInfo -and $processInfo.CommandLine -match "uvicorn.*server:app") {
    Stop-Process -Id $serverId -Force
    Write-Host "CIVICLENS server stopped." -ForegroundColor Cyan
} else {
    Write-Host "The recorded CIVICLENS server is not running." -ForegroundColor DarkGray
}
Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
