param([int]$Port = 30000)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot 'nexus\.venv\Scripts\python.exe'
if ($Port -lt 30000 -or $Port -gt 65535) { throw 'Port must be between 30000 and 65535.' }
if (Test-Path -LiteralPath $python) {
    & $python (Join-Path $PSScriptRoot 'run-nexus.py') --prepare --port $Port
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3.12 (Join-Path $PSScriptRoot 'run-nexus.py') --prepare --port $Port
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python (Join-Path $PSScriptRoot 'run-nexus.py') --prepare --port $Port
} else { throw 'Please install Python 3.12, then run start-nexus.cmd again.' }
if ($LASTEXITCODE -ne 0) { throw 'Python environment setup failed. See the output above.' }
$runtime = Join-Path $PSScriptRoot 'nexus\.runtime'
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
$probe = New-Object System.Net.Sockets.TcpClient
try { $probe.Connect('127.0.0.1', $Port); $occupied = $true } catch { $occupied = $false } finally { $probe.Dispose() }
if ($occupied) { throw "Port $Port is already in use. Visit http://127.0.0.1:$Port/ or choose another port." }
$process = Start-Process -FilePath $python -ArgumentList @('-m','uvicorn','nexus.backend.api.app:app','--host','127.0.0.1','--port',"$Port") -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtime 'server.log') -RedirectStandardError (Join-Path $runtime 'server-error.log') -PassThru
$process.Id | Set-Content (Join-Path $runtime 'server.pid')
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    try {
        $health = Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
        if ($health.status -eq 'ok') { Write-Host "NEXUS ready: http://127.0.0.1:$Port/"; exit 0 }
    } catch { }
    $process.Refresh()
    if ($process.HasExited) { throw "Startup failed. See nexus\.runtime\server-error.log" }
    Start-Sleep -Seconds 1
}
throw 'Startup timed out. See nexus\.runtime\server-error.log'
