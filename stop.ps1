$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$envFile = Join-Path $projectRoot '.env'
$port = 8020
if (Test-Path -LiteralPath $envFile) {
    $portLine = Get-Content -LiteralPath $envFile | Where-Object { $_ -match '^\s*PORT\s*=' } | Select-Object -Last 1
    if ($portLine -match '^\s*PORT\s*=\s*(\d+)') { $port = [int]$Matches[1] }
}
$pidFile = Join-Path $projectRoot 'var\server.pid'
if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Host 'Für dieses Projekt ist kein gestarteter Server vermerkt.'
    exit 0
}

$serverPid = 0
if (-not [int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$serverPid)) {
    Remove-Item -LiteralPath $pidFile -Force
    Write-Host 'Ungültige lokale PID-Datei entfernt.'
    exit 0
}
$processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $serverPid" -ErrorAction SilentlyContinue
$pythonPath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot '.venv\Scripts\python.exe'))
$processPath = if ($processInfo -and $processInfo.ExecutablePath) { [System.IO.Path]::GetFullPath($processInfo.ExecutablePath) } else { '' }
$ownedCommand = $processInfo -and ($processInfo.CommandLine -match 'uvicorn') -and ($processInfo.CommandLine -match "--port\s+$port(\s|$)")
if ($processInfo -and $processPath -ieq $pythonPath -and $ownedCommand) {
    Stop-Process -Id $serverPid -Force
    Write-Host "52W High Research auf Port $port wurde beendet."
} else {
    Write-Host 'Der Prozess gehört nicht eindeutig zu diesem Projekt und wurde nicht beendet.'
}
Remove-Item -LiteralPath $pidFile -Force
