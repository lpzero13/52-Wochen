param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
Set-Location -LiteralPath $projectRoot

$envFile = Join-Path $projectRoot '.env'
$exampleFile = Join-Path $projectRoot '.env.example'
if (-not (Test-Path -LiteralPath $envFile)) {
    Copy-Item -LiteralPath $exampleFile -Destination $envFile
    Write-Host 'Lokale Einstellungen aus .env.example angelegt.'
}

$port = 8020
$portLine = Get-Content -LiteralPath $envFile | Where-Object { $_ -match '^\s*PORT\s*=' } | Select-Object -Last 1
if ($portLine -match '^\s*PORT\s*=\s*(\d+)') { $port = [int]$Matches[1] }
if ($port -lt 1 -or $port -gt 65535) { throw "Ungültiger Port in .env: $port" }

$varDir = Join-Path $projectRoot 'var'
New-Item -ItemType Directory -Path $varDir -Force | Out-Null
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        & $pyLauncher.Source -3 -m venv (Join-Path $projectRoot '.venv')
    } else {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pythonCommand) { throw 'Python 3.11 oder neuer wurde nicht gefunden.' }
        & $pythonCommand.Source -m venv (Join-Path $projectRoot '.venv')
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython)) { throw 'Die lokale Python-Umgebung konnte nicht erstellt werden.' }
}
& $venvPython -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'
if ($LASTEXITCODE -ne 0) { throw '52W High Research benötigt Python 3.11 oder neuer.' }

& $venvPython -c 'import importlib.util; raise SystemExit(0 if all(importlib.util.find_spec(name) for name in ("fastapi", "uvicorn", "mcp", "yfinance", "pandas", "openpyxl", "exchange_calendars", "scipy", "sklearn", "tzdata")) else 1)'
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installiere die benötigten Projektpakete …'
    $pipOut = Join-Path $varDir 'pip-install.log'
    $pipErr = Join-Path $varDir 'pip-install-error.log'
    $requirementsPath = Join-Path $projectRoot 'requirements.txt'
    $pipArgs = "-m pip install -r `"$requirementsPath`""
    $pipProcess = Start-Process -FilePath $venvPython -ArgumentList $pipArgs -WorkingDirectory $projectRoot -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput $pipOut -RedirectStandardError $pipErr
    if ($pipProcess.ExitCode -ne 0) {
        $pipLog = if (Test-Path -LiteralPath $pipErr) { (Get-Content -LiteralPath $pipErr -Tail 30) -join [Environment]::NewLine } else { 'Kein Installationsprotokoll vorhanden.' }
        throw "Die Python-Pakete konnten nicht installiert werden:`n$pipLog"
    }
}

$listener = $null
try {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $port)
    $listener.Start()
} catch {
    throw "Port $port ist belegt. Prüfe, ob 52W High Research bereits läuft, oder ändere PORT in .env."
} finally {
    if ($listener) { $listener.Stop() }
}

$stdout = Join-Path $varDir 'server.log'
$stderr = Join-Path $varDir 'server-error.log'
$pidFile = Join-Path $varDir 'server.pid'
Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue
$process = Start-Process -FilePath $venvPython `
    -ArgumentList @('-m', 'uvicorn', 'backend.api:app', '--host', '127.0.0.1', '--port', "$port", '--log-level', 'info') `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -PassThru
Set-Content -LiteralPath $pidFile -Value $process.Id -Encoding ascii

$url = "http://127.0.0.1:$port"
$ready = $false
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    if ($process.HasExited) { break }
    try {
        $response = Invoke-RestMethod -Uri "$url/api/health" -TimeoutSec 2
        if ($response.ok) { $ready = $true; break }
    } catch { Start-Sleep -Milliseconds 500 }
}

if (-not $ready) {
    if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    $logText = if (Test-Path -LiteralPath $stderr) { (Get-Content -LiteralPath $stderr -Tail 25) -join [Environment]::NewLine } else { 'Kein Serverprotokoll vorhanden.' }
    throw "Der lokale Scanner konnte nicht starten. Serverprotokoll:`n$logText"
}

Write-Host "52W High Research läuft unter $url"
Write-Host 'Norgate-Historie und kostenloser aktueller Scanner-Cache werden getrennt in der App angezeigt.'
if (-not $NoBrowser) { Start-Process $url }
