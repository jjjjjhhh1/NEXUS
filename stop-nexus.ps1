$ErrorActionPreference = 'Stop'
$pidFile = Join-Path $PSScriptRoot 'nexus\.runtime\server.pid'
if (Test-Path -LiteralPath $pidFile) {
    $serverPid = [int](Get-Content -LiteralPath $pidFile)
    $server = Get-Process -Id $serverPid -ErrorAction SilentlyContinue
    if ($server -and $server.Path -eq (Join-Path $PSScriptRoot 'nexus\.venv\Scripts\python.exe')) {
        & taskkill.exe /PID $serverPid /T /F
        if ($LASTEXITCODE -ne 0) { throw 'Could not stop NEXUS.' }
        Write-Host 'NEXUS stopped.'
    }
    Remove-Item -LiteralPath $pidFile
}
