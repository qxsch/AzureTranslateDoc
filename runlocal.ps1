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

    # Find Azure OpenAI resource for glossary-enhanced translation
    $openaiResource = Get-AzCognitiveServicesAccount -ResourceGroupName $ResourceGroup |
                      Where-Object { $_.AccountType -eq "OpenAI" } |
                      Select-Object -First 1

    $openaiEndpoint = ""
    $openaiDeployment = "gpt-5.2-chat"
    $openaiApiKey = ""
    if ($openaiResource) {
        $openaiEndpoint = "https://$($openaiResource.AccountName).openai.azure.com"
        $openaiApiKey = (Get-AzCognitiveServicesAccountKey -ResourceGroupName $ResourceGroup -Name $openaiResource.AccountName).Key1
        Write-Host "  Azure OpenAI: $openaiEndpoint (deployment=$openaiDeployment)" -ForegroundColor Gray
        Write-Host ""
        Write-Host "  NOTE: For local Docker, Azure OpenAI uses API key auth." -ForegroundColor Yellow
        Write-Host "  Alternatively, set OPENAI_API_KEY in .env for direct OpenAI API access." -ForegroundColor Yellow
    } else {
        Write-Host "  No Azure OpenAI resource found in '$ResourceGroup'." -ForegroundColor Yellow
        Write-Host "  Enhanced translation will be unavailable unless you set OPENAI_API_KEY in .env." -ForegroundColor Yellow
    }

    # Ensure the directory exists
    $envDir = Split-Path $envFilePath -Parent
    if (-not (Test-Path $envDir)) { New-Item -ItemType Directory -Path $envDir -Force | Out-Null }

    $envLines = @(
        "AZURE_TRANSLATOR_ENDPOINT=$translatorEp"
        "AZURE_TRANSLATOR_KEY=$translatorKey"
        "AZURE_TRANSLATOR_REGION=$location"
        "AZURE_STORAGE_ACCOUNT_NAME=$storageAccountName"
        "AZURE_STORAGE_CONNECTION_STRING=$storageConnStr"
    )
    if ($openaiResource) {
        $envLines += "AZURE_OPENAI_ENDPOINT=$openaiEndpoint"
        $envLines += "AZURE_OPENAI_KEY=$openaiApiKey"
        $envLines += "AZURE_OPENAI_DEPLOYMENT=$openaiDeployment"
        # For local Docker, we pass the key directly since managed identity is not available
        $envLines += "# Azure OpenAI key for local auth (managed identity not available in Docker)"
        $envLines += "# To use OpenAI API instead, comment the above and set:"
        $envLines += "# OPENAI_API_KEY=sk-..."
    } else {
        $envLines += "# No Azure OpenAI found. For enhanced translation, set one of:"
        $envLines += "# AZURE_OPENAI_ENDPOINT=https://<name>.openai.azure.com"
        $envLines += "# AZURE_OPENAI_DEPLOYMENT=gpt-5.2-chat"
        $envLines += "# --- OR ---"
        $envLines += "# OPENAI_API_KEY=sk-..."
    }

    $envLines | Set-Content -Path $envFilePath -Encoding utf8

    Write-Host "  $EnvFile created with Translator + Storage + OpenAI credentials." -ForegroundColor Green
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
