param(
    [string]$Password = $env:ACCESS_SELF_CHECK_PASSWORD
)

$ErrorActionPreference = "Stop"

$RootDir = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location -LiteralPath $RootDir

function Read-EnvValue {
    param(
        [string]$Name,
        [string]$DefaultValue = ""
    )
    $envPath = Join-Path $RootDir ".env"
    if (-not (Test-Path -LiteralPath $envPath)) {
        return $DefaultValue
    }
    foreach ($line in Get-Content -LiteralPath $envPath) {
        if ($line -match "^\s*$([regex]::Escape($Name))\s*=\s*(.*)\s*$") {
            return $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
    return $DefaultValue
}

if ([string]::IsNullOrWhiteSpace($Password)) {
    $Password = "hhs54666"
}

$Port = Read-EnvValue -Name "PORT" -DefaultValue "8010"
if ([string]::IsNullOrWhiteSpace($Port)) {
    $Port = "8010"
}
$Url = "http://127.0.0.1:$Port"
$PythonBin = Join-Path $RootDir "backend\.venv\Scripts\python.exe"

Write-Host "[1/5] Checking files"
if (-not (Test-Path -LiteralPath ".env")) {
    throw ".env not found"
}
if (-not (Test-Path -LiteralPath $PythonBin)) {
    throw "backend/.venv Python not found"
}
if (-not (Test-Path -LiteralPath "frontend\dist\index.html")) {
    throw "frontend/dist/index.html not found"
}

Write-Host "[2/5] Checking Python imports"
& $PythonBin -m compileall -q backend/app backend/run.py
if ($LASTEXITCODE -ne 0) {
    throw "Python compileall failed"
}
& $PythonBin -c "import openai, httpx; print(f'openai {openai.__version__}'); print(f'httpx {httpx.__version__}')"
if ($LASTEXITCODE -ne 0) {
    throw "Python import check failed"
}

Write-Host "[3/5] Checking service process"
$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
try {
    Invoke-WebRequest `
        -UseBasicParsing `
        -Uri "$Url/auth/login" `
        -Method Post `
        -Body @{ password = $Password; next = "/" } `
        -WebSession $session `
        -MaximumRedirection 5 | Out-Null
} catch {
    throw "Login check failed or service is not reachable at $Url. Start it first with scripts/start_background.sh or backend/.venv/Scripts/python.exe backend/run.py. $($_.Exception.Message)"
}

try {
    $health = Invoke-WebRequest -UseBasicParsing -Uri "$Url/api/health" -WebSession $session
} catch {
    throw "Health check failed at $Url/api/health. $($_.Exception.Message)"
}
Write-Host $health.Content

Write-Host "[4/5] Checking frontend"
$index = Invoke-WebRequest -UseBasicParsing -Uri "$Url/" -WebSession $session
if ($index.Content -notmatch "GPT Image Studio") {
    throw "Frontend index did not contain GPT Image Studio"
}

Write-Host "[5/5] Checking settings APIs"
$settings = (Invoke-WebRequest -UseBasicParsing -Uri "$Url/api/settings" -WebSession $session).Content | ConvertFrom-Json
[pscustomobject]@{
    base_url = $settings.base_url
    api_key_configured = -not [string]::IsNullOrWhiteSpace($settings.api_key)
} | ConvertTo-Json -Compress | Write-Host

$providers = (Invoke-WebRequest -UseBasicParsing -Uri "$Url/api/providers" -WebSession $session).Content | ConvertFrom-Json
if ($null -eq $providers.items) {
    throw "Providers API did not return items"
}

$appSettings = (Invoke-WebRequest -UseBasicParsing -Uri "$Url/api/app-settings" -WebSession $session).Content | ConvertFrom-Json
if ($null -eq $appSettings.value) {
    throw "App settings API did not return value"
}

Write-Host "Self check passed."
