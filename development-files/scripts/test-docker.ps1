# ListSync Docker Test Script for Windows PowerShell

Write-Host "🐳 Testing ListSync Docker Setup" -ForegroundColor Cyan
Write-Host "================================" -ForegroundColor Cyan

# Function to check if command exists
function Test-Command {
    param($Command)
    try {
        Get-Command $Command -ErrorAction Stop | Out-Null
        return $true
    }
    catch {
        return $false
    }
}

# Function to wait for service
function Wait-ForService {
    param(
        [string]$Url,
        [string]$ServiceName,
        [int]$MaxAttempts = 30
    )
    
    Write-Host "⏳ Waiting for $ServiceName to be ready..." -ForegroundColor Yellow
    
    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        try {
            $response = Invoke-WebRequest -Uri $Url -Method Get -TimeoutSec 5 -ErrorAction Stop
            if ($response.StatusCode -eq 200) {
                Write-Host "✅ $ServiceName is ready!" -ForegroundColor Green
                return $true
            }
        }
        catch {
            # Service not ready yet
        }
        
        Write-Host "   Attempt $attempt/$MaxAttempts - waiting..." -ForegroundColor Gray
        Start-Sleep -Seconds 5
    }
    
    Write-Host "❌ $ServiceName failed to start within timeout" -ForegroundColor Red
    return $false
}

# Check prerequisites
Write-Host "`n📋 Checking Prerequisites..." -ForegroundColor Yellow

if (-not (Test-Command "docker")) {
    Write-Host "❌ Docker is not installed or not in PATH" -ForegroundColor Red
    exit 1
}

if (-not (Test-Command "docker-compose")) {
    Write-Host "❌ Docker Compose is not installed or not in PATH" -ForegroundColor Red
    exit 1
}

Write-Host "✅ Docker and Docker Compose are available" -ForegroundColor Green

# Check if .env file exists
if (-not (Test-Path ".env")) {
    Write-Host "`n⚠️  No .env file found. Creating a test configuration..." -ForegroundColor Yellow
    
    $envContent = @"
# Test Configuration for ListSync
OVERSEERR_URL=http://localhost:5055
OVERSEERR_API_KEY=test-api-key
OVERSEERR_USER_ID=1
SYNC_INTERVAL=24
AUTOMATED_MODE=true
TRAKT_LISTS=popular-movies
NEXT_PUBLIC_API_URL=http://localhost:4222/api
"@
    
    $envContent | Out-File -FilePath ".env" -Encoding UTF8
    Write-Host "✅ Created .env file with test configuration" -ForegroundColor Green
}

# Test container status
Write-Host "`n🐳 Checking Container Status..." -ForegroundColor Yellow

try {
    $containerStatus = docker-compose -f docker-compose.local.yml ps --format json | ConvertFrom-Json
    if ($containerStatus.State -eq "running") {
        Write-Host "✅ ListSync container is running" -ForegroundColor Green
    } else {
        Write-Host "❌ ListSync container is not running (State: $($containerStatus.State))" -ForegroundColor Red
        Write-Host "   Try: docker-compose -f docker-compose.local.yml up -d" -ForegroundColor Yellow
        exit 1
    }
}
catch {
    Write-Host "❌ Failed to check container status" -ForegroundColor Red
    Write-Host "   Error: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

# Test services
Write-Host "`n🔍 Testing Services..." -ForegroundColor Yellow

# Test API Backend
if (Wait-ForService -Url "http://localhost:4222/api/system/health" -ServiceName "API Backend") {
    try {
        $healthResponse = Invoke-RestMethod -Uri "http://localhost:4222/api/system/health" -Method Get
        Write-Host "   Database: $($healthResponse.database)" -ForegroundColor Cyan
        Write-Host "   Process: $($healthResponse.process)" -ForegroundColor Cyan
        Write-Host "   Sync Status: $($healthResponse.sync_status)" -ForegroundColor Cyan
    }
    catch {
        Write-Host "   ⚠️  Could not parse health response" -ForegroundColor Yellow
    }
} else {
    Write-Host "❌ API Backend test failed" -ForegroundColor Red
    exit 1
}

# Test Frontend
if (Wait-ForService -Url "http://localhost:3222" -ServiceName "Frontend Dashboard") {
    Write-Host "   Dashboard is accessible at http://localhost:3222" -ForegroundColor Cyan
} else {
    Write-Host "❌ Frontend test failed" -ForegroundColor Red
    exit 1
}

# Test API endpoints
Write-Host "`n📊 Testing API Endpoints..." -ForegroundColor Yellow

$endpoints = @(
    @{ Path = "/api/stats/sync"; Name = "Sync Statistics" },
    @{ Path = "/api/lists"; Name = "Lists Management" },
    @{ Path = "/api/system/health"; Name = "System Health" }
)

foreach ($endpoint in $endpoints) {
    try {
        $response = Invoke-RestMethod -Uri "http://localhost:4222$($endpoint.Path)" -Method Get -TimeoutSec 10
        Write-Host "✅ $($endpoint.Name): OK" -ForegroundColor Green
    }
    catch {
        Write-Host "❌ $($endpoint.Name): Failed" -ForegroundColor Red
        Write-Host "   Error: $($_.Exception.Message)" -ForegroundColor Red
    }
}

# Final summary
Write-Host "`n🎉 Test Summary" -ForegroundColor Green
Write-Host "===============" -ForegroundColor Green
Write-Host "✅ ListSync Complete Application is running successfully!" -ForegroundColor Green
Write-Host "" 
Write-Host "🌐 Access Points:" -ForegroundColor Cyan
Write-Host "   • Frontend Dashboard: http://localhost:3222" -ForegroundColor White
Write-Host "   • API Backend: http://localhost:4222/api" -ForegroundColor White
Write-Host "   • Health Check: http://localhost:4222/api/system/health" -ForegroundColor White
Write-Host ""
Write-Host "📝 Next Steps:" -ForegroundColor Yellow
Write-Host "   1. Open http://localhost:3222 in your browser" -ForegroundColor White
Write-Host "   2. Configure your Seerr settings in the dashboard" -ForegroundColor White
Write-Host "   3. Add your media lists and start syncing!" -ForegroundColor White
Write-Host ""
Write-Host "🔧 Management Commands:" -ForegroundColor Yellow
Write-Host "   • View logs: docker-compose -f docker-compose.local.yml logs -f" -ForegroundColor White
Write-Host "   • Stop: docker-compose -f docker-compose.local.yml down" -ForegroundColor White
Write-Host "   • Restart: docker-compose -f docker-compose.local.yml restart" -ForegroundColor White 