$appUrl = "http://127.0.0.1:8000"
try {
    Invoke-RestMethod -Uri "$appUrl/api/health" -TimeoutSec 2 | Out-Null
    Start-Process $appUrl
} catch {
    Write-Host "CIVICLENS is not running yet. Start it with .\start_civiclens.ps1, then run this script again." -ForegroundColor Yellow
}
