<#
.SYNOPSIS
  Build and run the TranslateDoc container locally.

.DESCRIPTION
  Builds the Docker image and runs it interactively on port 8888.
  If .env does not exist, fetches Translator credentials from
  Azure (requires Az PowerShell module and an active login) and creates it.

.PARAMETER Port
  Local port to expose the app on (default: 8888).

.PARAMETER ResourceGroup
  Azure resource group where TranslateDoc is deployed (for .env creation).

.PARAMETER AppName
  Base name used during deployment (for .env creation).

.PARAMETER EnvFile
  Path to the .env file relative to the script root.
#>
param(
    [int]$Port          = 8888,
    [string]$ResourceGroup = "rg-translatedoc",
    [string]$AppName       = "translatedoc",
    [string]$EnvFile       = ".env"
)

$ErrorActionPreference = "Stop"
$imageName = "translatedoc:local"
$envFilePath = Join-Path $PSScriptRoot $EnvFile

Write-Host ""
Write-Host "=====================================" -ForegroundColor Cyan
Write-Host "  TranslateDoc – Local Docker Run"     -ForegroundColor Cyan
Write-Host "=====================================" -ForegroundColor Cyan
Write-Host ""

# -------------------------------------------------------------------
# 0. Create .env from Azure if it doesn't exist
# -------------------------------------------------------------------
if (-not (Test-Path $envFilePath)) {
    Write-Host "[0/2] Creating $EnvFile from Azure resources..." -ForegroundColor Yellow

    if (-not (Get-Module -ListAvailable -Name Az.Accounts)) {
        throw "Az PowerShell module not found and $EnvFile is missing. Install with:  Install-Module Az -Scope CurrentUser"
    }

    $context = Get-AzContext -ErrorAction SilentlyContinue
    if (-not $context) {
        Write-Host "  Not logged in. Running Connect-AzAccount..." -ForegroundColor Gray
        Connect-AzAccount
        $context = Get-AzContext
    }

    # Find Translator Cognitive Services account in the resource group
    $translator = Get-AzCognitiveServicesAccount -ResourceGroupName $ResourceGroup |
                  Where-Object { $_.AccountType -eq "TextTranslation" } |
                  Select-Object -First 1

    if (-not $translator) {
        throw "No Translator (TextTranslation) resource found in resource group '$ResourceGroup'. Run deploy.ps1 first."
    }

    $translatorKey = (Get-AzCognitiveServicesAccountKey -ResourceGroupName $ResourceGroup -Name $translator.AccountName).Key1
    $location      = $translator.Location

    # The Document Translation API requires a CUSTOM DOMAIN endpoint,
    # not the global api.cognitive.microsofttranslator.com endpoint.
    # Construct it from the custom subdomain name (= account name set in Bicep).
    $customDomain = $translator.AccountName
    $translatorEp = "https://$customDomain.cognitiveservices.azure.com"
    Write-Host "  Using custom-domain endpoint: $translatorEp" -ForegroundColor Gray

    # Find Storage Account for batch translation (name starts with 'sttranslate')
    $storageAccount = Get-AzStorageAccount -ResourceGroupName $ResourceGroup |
                      Where-Object { $_.StorageAccountName -like "sttranslate*" } |
                      Select-Object -First 1

    $storageConnStr = ""
    $storageAccountName = ""
    if ($storageAccount) {
        $storageAccountName = $storageAccount.StorageAccountName
        $storageKeys = Get-AzStorageAccountKey -ResourceGroupName $ResourceGroup -Name $storageAccountName
        $storageConnStr = "DefaultEndpointsProtocol=https;AccountName=$storageAccountName;AccountKey=$($storageKeys[0].Value);EndpointSuffix=core.windows.net"
        Write-Host "  Storage account: $storageAccountName" -ForegroundColor Gray
    } else {
        Write-Host "  WARNING: No storage account found – batch translation (PDF, DOCX) will not work." -ForegroundColor Yellow
    }

    # Ensure the directory exists
    $envDir = Split-Path $envFilePath -Parent
    if (-not (Test-Path $envDir)) { New-Item -ItemType Directory -Path $envDir -Force | Out-Null }

    @(
        "AZURE_TRANSLATOR_ENDPOINT=$translatorEp"
        "AZURE_TRANSLATOR_KEY=$translatorKey"
        "AZURE_TRANSLATOR_REGION=$location"
        "AZURE_STORAGE_ACCOUNT_NAME=$storageAccountName"
        "AZURE_STORAGE_CONNECTION_STRING=$storageConnStr"
    ) | Set-Content -Path $envFilePath -Encoding utf8

    Write-Host "  $EnvFile created with Translator + Storage credentials." -ForegroundColor Green
} else {
    Write-Host "[0/2] $EnvFile already exists – using existing file." -ForegroundColor Gray
}

# -------------------------------------------------------------------
# 1. Build
# -------------------------------------------------------------------
Write-Host "[1/2] Building Docker image..." -ForegroundColor Yellow
docker build -t $imageName $PSScriptRoot
if ($LASTEXITCODE -ne 0) { throw "Docker build failed" }
Write-Host "  Image built: $imageName" -ForegroundColor Green

# -------------------------------------------------------------------
# 2. Run
# -------------------------------------------------------------------
Write-Host "[2/2] Starting container on http://localhost:$Port ..." -ForegroundColor Yellow

$runArgs = @("run", "--rm", "-it", "-p", "${Port}:8000", "--env-file", $envFilePath, $imageName)

Write-Host ""
Write-Host "  Open http://localhost:$Port in your browser." -ForegroundColor White
Write-Host "  Press Ctrl+C to stop." -ForegroundColor Gray
Write-Host ""

& docker @runArgs
