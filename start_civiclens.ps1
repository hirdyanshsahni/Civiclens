param(
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$backendPath = Join-Path $projectRoot "backend"
$dataPath = Join-Path $backendPath "data"
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$pidPath = Join-Path $projectRoot ".civiclens.pid"
$stdoutPath = Join-Path $dataPath "server.log"
$stderrPath = Join-Path $dataPath "server-error.log"

if (Test-Path -LiteralPath $venvPython) {
    $pythonPath = $venvPython
} else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) { $pythonCommand = Get-Command py -ErrorAction SilentlyContinue }
    if (-not $pythonCommand) {
        Write-Error "Python was not found. Install Python 3.10+ or follow the setup steps in README.md."
        exit 1
    }
    $pythonPath = $pythonCommand.Source
}

if (Test-Path -LiteralPath $pidPath) {
    $savedId = 0
    [int]::TryParse((Get-Content -LiteralPath $pidPath -Raw).Trim(), [ref]$savedId) | Out-Null
    if ($savedId -gt 0) {
        $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($listener -and $listener.OwningProcess -eq $savedId) {
            try {
                $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
                if ($health.status -eq "ok" -and $health.database -eq "sqlite") {
                    Write-Host "CIVICLENS is already running (PID $savedId) at http://127.0.0.1:$Port." -ForegroundColor Yellow
                    exit 0
                }
            } catch { }
        }
    }
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
}

Write-Host "Starting CIVICLENS at http://127.0.0.1:$Port" -ForegroundColor Cyan
New-Item -ItemType Directory -Path $dataPath -Force | Out-Null
$portProbe = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $Port)
try {
    $portProbe.Start()
    $portProbe.Stop()
} catch {
    $portProbe.Stop()
    Write-Error "Port $Port is already in use. Choose another port with -Port."
    exit 1
}

$server = Start-Process -FilePath $pythonPath -ArgumentList @("-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", "$Port") -WorkingDirectory $backendPath -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
$server.Id | Set-Content -LiteralPath $pidPath -Encoding ascii

$ready = $false
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    Start-Sleep -Milliseconds 500
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
        if ($health.status -eq "ok") { $ready = $true; break }
    } catch { }
    if ($server.HasExited) { break }
}

if (-not $ready) {
    if (-not $server.HasExited) { Stop-Process -Id $server.Id -Force }
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $stderrPath) { Get-Content -LiteralPath $stderrPath -Tail 12 | Write-Host }
    Write-Error "CIVICLENS did not start. Check the error log at backend\data\server-error.log."
    exit 1
}

Start-Process "http://127.0.0.1:$Port"
Write-Host "CIVICLENS is ready. Run .\stop_civiclens.ps1 when you are finished." -ForegroundColor Green
