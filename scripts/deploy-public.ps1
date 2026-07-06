# Deploy ops-monitor for sharing (single port + optional public tunnel)
# Usage:
#   .\scripts\deploy-public.ps1              # build + start on http://0.0.0.0:8001
#   .\scripts\deploy-public.ps1 -Tunnel       # also start Cloudflare quick tunnel (public URL)

param(
    [switch]$Tunnel,
    [int]$Port = 8001
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$Node = "C:\Users\WJCZN\AppData\Local\Programs\cursor\resources\app\resources\helpers\node.exe"
if (-not (Test-Path $Node)) {
    $Node = (Get-Command node -ErrorAction SilentlyContinue).Source
}
if (-not $Node) { throw "Node.js not found. Install Node or use Cursor bundled node." }

Write-Host "==> Building frontend..." -ForegroundColor Cyan
Push-Location $Frontend
& $Node .\node_modules\vite\bin\vite.js build
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "Frontend build failed" }
Pop-Location

Write-Host "==> Starting backend on port $Port (all interfaces)..." -ForegroundColor Cyan
$env:OPS_MONITOR_RELOAD = "0"
$backendJob = Start-Job -ScriptBlock {
    param($Backend, $Port)
    Set-Location $Backend
    & .\.venv\Scripts\Activate.ps1
    $env:OPS_MONITOR_RELOAD = "0"
    python -m uvicorn app.main:app --host 0.0.0.0 --port $Port
} -ArgumentList $Backend, $Port

Start-Sleep -Seconds 4
try {
    $health = Invoke-RestMethod -Uri "http://localhost:$Port/api/health" -TimeoutSec 10
    Write-Host "==> Local URL:  http://localhost:$Port" -ForegroundColor Green
    Write-Host "==> LAN URL:    http://$((Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.PrefixOrigin -ne 'WellKnown' } | Select-Object -First 1).IPAddress):$Port" -ForegroundColor Green
} catch {
    Write-Host "Backend may still be starting. Check: http://localhost:$Port/api/health" -ForegroundColor Yellow
}

if ($Tunnel) {
    $cf = Get-Command cloudflared -ErrorAction SilentlyContinue
    if (-not $cf) {
        Write-Host "==> Installing cloudflared via winget..." -ForegroundColor Cyan
        winget install Cloudflare.cloudflared --accept-package-agreements --accept-source-agreements
        $cf = Get-Command cloudflared -ErrorAction SilentlyContinue
    }
    if ($cf) {
        Write-Host "==> Starting public tunnel (share the https URL with your manager)..." -ForegroundColor Cyan
        Write-Host "    Press Ctrl+C to stop tunnel and backend." -ForegroundColor DarkGray
        & cloudflared tunnel --url "http://localhost:$Port"
    } else {
        Write-Host "Could not install cloudflared. Install manually: winget install Cloudflare.cloudflared" -ForegroundColor Red
        Write-Host "Then run: cloudflared tunnel --url http://localhost:$Port" -ForegroundColor Yellow
    }
} else {
    Write-Host ""
    Write-Host "For a public shareable URL (recommended on VDI), run:" -ForegroundColor Yellow
    Write-Host "  cloudflared tunnel --url http://localhost:$Port" -ForegroundColor White
    Write-Host ""
    Write-Host "Backend is running in background job. To stop: Get-Job | Stop-Job; Get-Job | Remove-Job" -ForegroundColor DarkGray
    Write-Host "Or run with -Tunnel flag: .\scripts\deploy-public.ps1 -Tunnel" -ForegroundColor DarkGray
}
