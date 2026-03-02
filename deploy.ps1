<#
.SYNOPSIS
  One-command deployment of TranslateDoc to Azure using Az PowerShell module.

.DESCRIPTION
  Creates all Azure resources (Translator, ACR, Container App) and deploys the
  application with Entra ID authentication.  Requires the Az, Az.App, and
  Az.ContainerRegistry PowerShell modules.

.PARAMETER ResourceGroup
  Name of the Azure resource group (created if not exists).

.PARAMETER Location
  Azure region for all resources.

.PARAMETER AppName
  Base name for all resources.

.PARAMETER localDockerBuild
  Use local Docker build and push instead of ACR cloud build.
  Requires Docker Desktop running locally.
#>
param(
    [string]$ResourceGroup  = "rg-translatedoc",
    [string]$Location       = "switzerlandnorth",
    [string]$AppName        = "translatedoc",
    [switch]$localDockerBuild
)

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "=====================================" -ForegroundColor Cyan
Write-Host "  TranslateDoc – Azure Deployment"     -ForegroundColor Cyan
Write-Host "  (Az PowerShell module)"              -ForegroundColor Cyan
Write-Host "=====================================" -ForegroundColor Cyan
Write-Host ""

# -------------------------------------------------------------------
# 0. Pre-flight checks
# -------------------------------------------------------------------
Write-Host "[0/7] Checking prerequisites..." -ForegroundColor Yellow

# Ensure Az module is available
if (-not (Get-Module -ListAvailable -Name Az.Accounts)) {
    Write-Error "Az PowerShell module not found.  Install with:  Install-Module Az -Scope CurrentUser"
    exit 1
}

# Check login state
$context = Get-AzContext -ErrorAction SilentlyContinue
if (-not $context) {
    Write-Host "  Not logged in. Running Connect-AzAccount..." -ForegroundColor Gray
    Connect-AzAccount
    $context = Get-AzContext
}

$tenantId = $context.Tenant.Id
Write-Host "  Tenant:       $tenantId" -ForegroundColor Gray
Write-Host "  Subscription: $($context.Subscription.Name)" -ForegroundColor Gray

# -------------------------------------------------------------------
# 1. Resource group
# -------------------------------------------------------------------
Write-Host "[1/7] Creating resource group '$ResourceGroup' in '$Location'..." -ForegroundColor Yellow
New-AzResourceGroup -Name $ResourceGroup -Location $Location -Force | Out-Null

# -------------------------------------------------------------------
# 2. Entra ID app registration
# -------------------------------------------------------------------
Write-Host "[2/7] Creating Entra ID app registration..." -ForegroundColor Yellow

$displayName = "${AppName}-auth"
$existingApp = Get-AzADApplication -DisplayName $displayName -ErrorAction SilentlyContinue | Select-Object -First 1

if ($existingApp) {
    Write-Host "  App registration already exists: $($existingApp.AppId)" -ForegroundColor Gray
    $clientId = $existingApp.AppId
    $objectId = $existingApp.Id
    # Ensure ID token issuance is enabled (may have been missed in a prior failed run)
    Update-AzADApplication -ObjectId $objectId -Web @{
        ImplicitGrantSetting = @{ EnableIdTokenIssuance = $true }
    }
} else {
    $webSettings = @{
        RedirectUri            = @("https://localhost:4433/.auth/login/aad/callback")
        ImplicitGrantSetting   = @{ EnableIdTokenIssuance = $true }
    }
    $newApp = New-AzADApplication `
        -DisplayName $displayName `
        -SignInAudience AzureADMyOrg `
        -Web $webSettings

    $clientId = $newApp.AppId
    $objectId = $newApp.Id
    Write-Host "  Created: $clientId" -ForegroundColor Gray

    # Create service principal
    try {
        New-AzADServicePrincipal -ApplicationId $clientId | Out-Null
    } catch {
        Write-Host "  Service principal already exists or could not be created: $($_.Exception.Message)" -ForegroundColor Gray
    }
}

# Create / rotate client secret (2 years)
$endDate = (Get-Date).AddYears(2)
$credential = New-AzADAppCredential -ObjectId $objectId -EndDate $endDate
$clientSecret = $credential.SecretText
Write-Host "  Client secret created." -ForegroundColor Gray

# -------------------------------------------------------------------
# 3. Deploy Bicep infrastructure
# -------------------------------------------------------------------
Write-Host "[3/7] Deploying infrastructure (Bicep)..." -ForegroundColor Yellow

$deployment = New-AzResourceGroupDeployment `
    -ResourceGroupName $ResourceGroup `
    -TemplateFile "infra/main.bicep" `
    -appName $AppName `
    -entraClientId (ConvertTo-SecureString $clientId -AsPlainText -Force) `
    -entraClientSecret (ConvertTo-SecureString $clientSecret -AsPlainText -Force) `
    -Force

# Fetch outputs
$acrName        = $deployment.Outputs["acrName"].Value
$acrLogin       = $deployment.Outputs["acrLoginServer"].Value
$caName         = $deployment.Outputs["containerAppName"].Value
$fqdn           = $deployment.Outputs["containerAppFqdn"].Value
$translatorEp   = $deployment.Outputs["translatorEndpoint"].Value
$storageName    = $deployment.Outputs["storageAccountName"].Value

Write-Host "  ACR:         $acrLogin"       -ForegroundColor Gray
Write-Host "  App FQDN:    $fqdn"           -ForegroundColor Gray
Write-Host "  Translator:  $translatorEp"   -ForegroundColor Gray
Write-Host "  Storage:     $storageName"     -ForegroundColor Gray

# -------------------------------------------------------------------
# 4. Update redirect URI on app registration
# -------------------------------------------------------------------
Write-Host "[4/7] Updating Entra ID redirect URI..." -ForegroundColor Yellow

$redirectUri = "https://${fqdn}/.auth/login/aad/callback"
Update-AzADApplication -ObjectId $objectId -Web @{
    RedirectUri          = @($redirectUri)
    ImplicitGrantSetting = @{ EnableIdTokenIssuance = $true }
}

# -------------------------------------------------------------------
# 5. Build & push container image
# -------------------------------------------------------------------
if ($localDockerBuild) {
    Write-Host "[5/7] Building container image locally with Docker..." -ForegroundColor Yellow

    # Verify Docker is available
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker is not installed or not in PATH. Install Docker Desktop or remove -localDockerBuild to use ACR cloud build."
    }

    $imageRef = "${acrLogin}/${AppName}:latest"

    # Log in to ACR using AAD token exchange (no admin user required)
    Write-Host "  Logging in to ACR via AAD token exchange..." -ForegroundColor Gray
    $aadToken = (Get-AzAccessToken).Token
    $exchangeBody = @{
        grant_type   = "access_token"
        service      = $acrLogin
        access_token = $aadToken
    }
    $acrRefreshToken = (Invoke-RestMethod -Uri "https://${acrLogin}/oauth2/exchange" -Method POST -Body $exchangeBody).refresh_token
    $acrRefreshToken | docker login $acrLogin -u "00000000-0000-0000-0000-000000000000" --password-stdin
    if ($LASTEXITCODE -ne 0) { throw "Docker login to ACR failed" }

    # Build
    Write-Host "  Building image..." -ForegroundColor Gray
    docker build -t $imageRef $PSScriptRoot
    if ($LASTEXITCODE -ne 0) { throw "Docker build failed" }

    # Push
    Write-Host "  Pushing image to ACR..." -ForegroundColor Gray
    docker push $imageRef
    if ($LASTEXITCODE -ne 0) { throw "Docker push failed" }

    Write-Host "  Image built and pushed via local Docker." -ForegroundColor Green

}
else {
    Write-Host "[5/7] Building container image in ACR cloud build (this may take a few minutes)..." -ForegroundColor Yellow

    $subscriptionId = $context.Subscription.Id

    # 5a. Create tar.gz of the build context using Windows built-in tar.exe
    $tarFile = Join-Path $env:TEMP "acr-build-context.tar.gz"
    if (Test-Path $tarFile) { Remove-Item $tarFile -Force }

    Push-Location $PSScriptRoot
    try {
        & tar.exe -czf $tarFile --exclude=node_modules --exclude=.git --exclude=__pycache__ --exclude="*.pyc" .
        if ($LASTEXITCODE -ne 0) { throw "Failed to create build context archive" }
    } finally {
        Pop-Location
    }

    Write-Host "  Build context: $([math]::Round((Get-Item $tarFile).Length / 1MB, 1)) MB" -ForegroundColor Gray

    # 5b. Get upload URL from ACR
    $getBlobPath = "/subscriptions/$subscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.ContainerRegistry/registries/$acrName/listBuildSourceUploadUrl?api-version=2019-06-01-preview"
    $uploadResponse = Invoke-AzRestMethod -Method POST -Path $getBlobPath
    if ($uploadResponse.StatusCode -ne 200) { throw "Failed to get ACR upload URL: $($uploadResponse.Content)" }
    $uploadInfo    = $uploadResponse.Content | ConvertFrom-Json
    $uploadUrl     = $uploadInfo.uploadUrl
    $relativePath  = $uploadInfo.relativePath

    # 5c. Upload the tar.gz to the blob URL
    Write-Host "  Uploading build context..." -ForegroundColor Gray
    $fileBytes = [System.IO.File]::ReadAllBytes($tarFile)
    Invoke-RestMethod -Uri $uploadUrl -Method PUT -Headers @{
        "x-ms-blob-type" = "BlockBlob"
        "Content-Type"   = "application/octet-stream"
    } -Body $fileBytes
    Remove-Item $tarFile -Force

    # 5d. Schedule a Docker build run in ACR
    Write-Host "  Scheduling ACR cloud build..." -ForegroundColor Gray
    $scheduleRunPath = "/subscriptions/$subscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.ContainerRegistry/registries/$acrName/scheduleRun?api-version=2019-06-01-preview"
    $buildPayload = @{
        type           = "DockerBuildRequest"
        dockerFilePath = "Dockerfile"
        imageNames     = @("${AppName}:latest")
        sourceLocation = $relativePath
        platform       = @{ os = "Linux"; architecture = "amd64" }
        isPushEnabled  = $true
    } | ConvertTo-Json -Depth 5

    $runResponse = Invoke-AzRestMethod -Method POST -Path $scheduleRunPath -Payload $buildPayload
    if ($runResponse.StatusCode -notin @(200, 201, 202)) { throw "Failed to schedule ACR build: $($runResponse.Content)" }
    $runInfo = $runResponse.Content | ConvertFrom-Json
    $runId   = $runInfo.properties.runId

    Write-Host "  Build run started: $runId" -ForegroundColor Gray

    # 5e. Poll for build completion (up to 10 minutes)
    $getRunPath = "/subscriptions/$subscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.ContainerRegistry/registries/$acrName/runs/${runId}?api-version=2019-06-01-preview"
    $maxWait = 600
    $elapsed = 0
    $interval = 15

    do {
        Start-Sleep -Seconds $interval
        $elapsed += $interval
        $statusResponse = Invoke-AzRestMethod -Method GET -Path $getRunPath
        $statusInfo = $statusResponse.Content | ConvertFrom-Json
        $buildStatus = $statusInfo.properties.status
        Write-Host "  Build status: $buildStatus (${elapsed}s elapsed)..." -ForegroundColor Gray
    } while ($buildStatus -in @("Queued", "Started", "Running") -and $elapsed -lt $maxWait)

    if ($buildStatus -ne "Succeeded") {
        throw "ACR build failed with status: $buildStatus"
    }

    Write-Host "  Image built and pushed successfully." -ForegroundColor Green

} # end ACR cloud build branch

# -------------------------------------------------------------------
# 6. Update Container App with the real image (preserving env vars)
# -------------------------------------------------------------------
Write-Host "[6/7] Updating Container App with new image..." -ForegroundColor Yellow

$imageRef = "${acrLogin}/${AppName}:latest"

# Read current container app to preserve env vars, resources, and scale config
$ca = Get-AzContainerApp -ResourceGroupName $ResourceGroup -Name $caName
$existing = $ca.TemplateContainer | Where-Object { $_.Name -eq $AppName } | Select-Object -First 1

# Build container spec preserving existing configuration
$containerSpec = @{
    Name  = $AppName
    Image = $imageRef
}

# Convert SDK env-var objects to plain hashtables so Update-AzContainerApp accepts them
if ($existing.Env -and $existing.Env.Count -gt 0) {
    $envList = @()
    foreach ($e in $existing.Env) {
        $entry = @{ Name = $e.Name }
        if ($e.Value)     { $entry.Value     = $e.Value }
        if ($e.SecretRef) { $entry.SecretRef = $e.SecretRef }
        $envList += $entry
    }
    $containerSpec.Env = $envList
}

if ($existing.ResourceCpu)    { $containerSpec.ResourceCpu    = $existing.ResourceCpu }
if ($existing.ResourceMemory) { $containerSpec.ResourceMemory = $existing.ResourceMemory }

Update-AzContainerApp `
    -ResourceGroupName $ResourceGroup `
    -Name $caName `
    -TemplateContainer @($containerSpec) | Out-Null

# -------------------------------------------------------------------
# 7. Done!
# -------------------------------------------------------------------
Write-Host ""
Write-Host "=====================================" -ForegroundColor Green
Write-Host "  Deployment complete!" -ForegroundColor Green
Write-Host "=====================================" -ForegroundColor Green
Write-Host ""
Write-Host "  URL:  https://$fqdn" -ForegroundColor White
Write-Host ""
Write-Host "  All users in your Entra ID tenant can sign in." -ForegroundColor Gray
Write-Host ""
