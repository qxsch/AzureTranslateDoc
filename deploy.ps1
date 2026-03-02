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

.PARAMETER CustomDomain
  Optional custom domain name (e.g. translate.example.com).
  When set, the script validates DNS records (CNAME + TXT) and binds
  the domain with a managed TLS certificate.  Run without this
  parameter first, set up DNS, then re-run with -CustomDomain.
#>
param(
    [string]$ResourceGroup  = "rg-translatedoc",
    [string]$Location       = "swedencentral",
    [string]$AppName        = "translatedoc",
    [switch]$localDockerBuild,
    [string]$CustomDomain   = "",
    [switch]$CleanOldSecrets
)

$ErrorActionPreference = "Stop"

# Helper: PATCH a Container App with retry on ContainerAppOperationInProgress
function Invoke-ContainerAppPatch {
    param(
        [string]$Path,
        [string]$Payload,
        [string]$OperationLabel,
        [int]$MaxRetries = 12,
        [int]$RetryDelaySec = 15
    )
    for ($attempt = 1; $attempt -le $MaxRetries; $attempt++) {
        $resp = Invoke-AzRestMethod -Method PATCH -Path $Path -Payload $Payload
        if ($resp.StatusCode -in @(200, 201, 202)) {
            return $resp
        }
        $errBody = $resp.Content | ConvertFrom-Json -ErrorAction SilentlyContinue
        if ($errBody.error.code -eq 'ContainerAppOperationInProgress' -and $attempt -lt $MaxRetries) {
            Write-Host "  Operation in progress, retrying in ${RetryDelaySec}s... (attempt $attempt/$MaxRetries)" -ForegroundColor Gray
            Start-Sleep -Seconds $RetryDelaySec
        } else {
            throw "Failed to ${OperationLabel}: $($resp.Content)"
        }
    }
    throw "Failed to ${OperationLabel} after $MaxRetries attempts."
}

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
Write-Host "  Client secret created. KeyId: $($credential.KeyId)  Expires: $($credential.EndDateTime)" -ForegroundColor Gray

# -------------------------------------------------------------------
# 3. Deploy Bicep infrastructure
# -------------------------------------------------------------------
Write-Host "[3/7] Deploying infrastructure (Bicep)..." -ForegroundColor Yellow

# Pre-read existing custom domains from the Container App (if it exists)
# so Bicep can preserve them during redeployment.
$subscriptionId = $context.Subscription.Id
$existingCustomDomains = @()
$preAppRestPath = "/subscriptions/$subscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.App/containerApps/${AppName}?api-version=2024-03-01"
$preAppResp = Invoke-AzRestMethod -Method GET -Path $preAppRestPath
if ($preAppResp.StatusCode -eq 200) {
    $preAppInfo = $preAppResp.Content | ConvertFrom-Json
    if ($preAppInfo.properties.configuration.ingress.customDomains) {
        $existingCustomDomains = @($preAppInfo.properties.configuration.ingress.customDomains)
    }
    if ($existingCustomDomains.Count -gt 0) {
        Write-Host "  Preserving existing custom domains through Bicep deployment:" -ForegroundColor Gray
        foreach ($d in $existingCustomDomains) {
            Write-Host "    - $($d.name)" -ForegroundColor Gray
        }
    }
}

# Convert custom domains to the format Bicep expects
$bicepCustomDomains = @()
foreach ($d in $existingCustomDomains) {
    $entry = @{ name = $d.name; bindingType = $d.bindingType }
    if ($d.certificateId) { $entry.certificateId = $d.certificateId }
    $bicepCustomDomains += $entry
}

$deployment = New-AzResourceGroupDeployment `
    -ResourceGroupName $ResourceGroup `
    -TemplateFile "infra/main.bicep" `
    -appName $AppName `
    -entraClientId (ConvertTo-SecureString $clientId -AsPlainText -Force) `
    -entraClientSecret (ConvertTo-SecureString $clientSecret -AsPlainText -Force) `
    -customDomains $bicepCustomDomains `
    -Force

# Fetch outputs
$acrName        = $deployment.Outputs["acrName"].Value
$acrLogin       = $deployment.Outputs["acrLoginServer"].Value
$caName         = $deployment.Outputs["containerAppName"].Value
$fqdn           = $deployment.Outputs["containerAppFqdn"].Value
$envName        = $deployment.Outputs["containerAppEnvName"].Value
$translatorEp   = $deployment.Outputs["translatorEndpoint"].Value
$storageName    = $deployment.Outputs["storageAccountName"].Value

Write-Host "  ACR:         $acrLogin"       -ForegroundColor Gray
Write-Host "  App FQDN:    $fqdn"           -ForegroundColor Gray
Write-Host "  Translator:  $translatorEp"   -ForegroundColor Gray
Write-Host "  Storage:     $storageName"     -ForegroundColor Gray

# -------------------------------------------------------------------
# 3b. Discover existing custom domains & early DNS validation
# -------------------------------------------------------------------

# Read the Container App Environment for the domain verification ID
$caEnvPath = "/subscriptions/$subscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.App/managedEnvironments/${envName}?api-version=2024-03-01"
$caEnvResponse = Invoke-AzRestMethod -Method GET -Path $caEnvPath
if ($caEnvResponse.StatusCode -ne 200) {
    throw "Failed to read Container App Environment: $($caEnvResponse.Content)"
}
$caEnvInfo = $caEnvResponse.Content | ConvertFrom-Json
$verificationId = $caEnvInfo.properties.customDomainConfiguration.customDomainVerificationId

# Read existing custom domains already bound to the Container App (refresh after Bicep)
$appRestPath = "/subscriptions/$subscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.App/containerApps/${caName}?api-version=2024-03-01"
$appGetResp = Invoke-AzRestMethod -Method GET -Path $appRestPath
$appInfo = $appGetResp.Content | ConvertFrom-Json
$existingCustomDomains = @()
if ($appInfo.properties.configuration.ingress.customDomains) {
    $existingCustomDomains = @($appInfo.properties.configuration.ingress.customDomains)
}
$existingDomainNames = @($existingCustomDomains | ForEach-Object { $_.name })

if ($existingDomainNames.Count -gt 0) {
    Write-Host "  Active custom domains:" -ForegroundColor Gray
    foreach ($dn in $existingDomainNames) {
        Write-Host "    - $dn" -ForegroundColor Gray
    }
}

# Early DNS validation when -CustomDomain is specified — fail fast before build
if ($CustomDomain) {
    $existingEntry = $existingCustomDomains | Where-Object { $_.name -eq $CustomDomain } | Select-Object -First 1
    if ($existingEntry -and $existingEntry.bindingType -eq 'SniEnabled' -and $existingEntry.certificateId) {
        Write-Host "  Custom domain '$CustomDomain' is already bound with TLS — nothing new to add." -ForegroundColor Green
        $CustomDomain = ""   # clear to skip binding later; domain stays in existingDomainNames
    } elseif ($existingEntry) {
        Write-Host "  Custom domain '$CustomDomain' exists but binding is '$($existingEntry.bindingType)' — will repair in step 7." -ForegroundColor Yellow
    } else {
        Write-Host ""
        Write-Host "[DNS Check] Validating DNS for '$CustomDomain' before proceeding..." -ForegroundColor Yellow
        $dnsOk = $true

        # --- CNAME ---
        try {
            $cnameRecords = Resolve-DnsName -Name $CustomDomain -Type CNAME -DnsOnly -ErrorAction Stop
            $cnameTarget = ($cnameRecords | Where-Object { $_.QueryType -eq 'CNAME' } |
                            Select-Object -First 1).NameHost
            if ($cnameTarget) { $cnameTarget = $cnameTarget.TrimEnd('.') }
            $expectedFqdn = $fqdn.TrimEnd('.')
            if ($cnameTarget -ne $expectedFqdn) {
                Write-Host "  [FAIL] CNAME points to '$cnameTarget', expected '$expectedFqdn'" -ForegroundColor Red
                $dnsOk = $false
            } else {
                Write-Host "  [OK]   CNAME  $CustomDomain -> $fqdn" -ForegroundColor Green
            }
        } catch {
            Write-Host "  [FAIL] CNAME record not found for '$CustomDomain'" -ForegroundColor Red
            $dnsOk = $false
        }

        # --- TXT (asuid.<domain>) ---
        $txtHost = "asuid.$CustomDomain"
        try {
            $txtRecords = Resolve-DnsName -Name $txtHost -Type TXT -DnsOnly -ErrorAction Stop
            $txtValues  = ($txtRecords | Where-Object { $_.QueryType -eq 'TXT' }).Strings
            if ($txtValues -contains $verificationId) {
                Write-Host "  [OK]   TXT    $txtHost = $verificationId" -ForegroundColor Green
            } else {
                Write-Host "  [FAIL] TXT value mismatch. Got '$($txtValues -join ', ')', expected '$verificationId'" -ForegroundColor Red
                $dnsOk = $false
            }
        } catch {
            Write-Host "  [FAIL] TXT record not found for '$txtHost'" -ForegroundColor Red
            $dnsOk = $false
        }

        if (-not $dnsOk) {
            Write-Host ""
            Write-Host "  DNS is not correctly configured yet. Stopping before build." -ForegroundColor Red
            Write-Host "  Please create these records with your external DNS provider:" -ForegroundColor Yellow
            Write-Host ""
            Write-Host "    Type   Host                              Value" -ForegroundColor White
            Write-Host "    -----  --------------------------------  ----------------------------------------" -ForegroundColor Gray
            Write-Host "    CNAME  $CustomDomain" -ForegroundColor White -NoNewline
            Write-Host ("  " + $fqdn) -ForegroundColor Cyan
            Write-Host "    TXT    asuid.$CustomDomain" -ForegroundColor White -NoNewline
            Write-Host ("  " + $verificationId) -ForegroundColor Cyan
            Write-Host ""
            Write-Host "  After DNS propagation, re-run:" -ForegroundColor Yellow
            Write-Host "    .\deploy.ps1 -CustomDomain $CustomDomain" -ForegroundColor White
            Write-Host ""
            exit 1
        }

        Write-Host "  DNS validated. Will bind domain after image deployment." -ForegroundColor Green
        Write-Host ""
    }
}

# -------------------------------------------------------------------
# 4. Update Entra ID redirect URIs (default + ALL custom domains)
# -------------------------------------------------------------------
Write-Host "[4/7] Updating Entra ID redirect URIs..." -ForegroundColor Yellow

# Build comprehensive list: default FQDN + every active custom domain + new domain
$allHostnames = @($fqdn)
foreach ($dn in $existingDomainNames) {
    if ($allHostnames -notcontains $dn) { $allHostnames += $dn }
}
if ($CustomDomain -and ($allHostnames -notcontains $CustomDomain)) {
    $allHostnames += $CustomDomain
}

$redirectUris = @()
foreach ($hostname in $allHostnames) {
    $redirectUris += "https://${hostname}/.auth/login/aad/callback"
}

Write-Host "  Redirect URIs:" -ForegroundColor Gray
foreach ($uri in $redirectUris) {
    Write-Host "    $uri" -ForegroundColor Gray
}

Update-AzADApplication -ObjectId $objectId -Web @{
    RedirectUri          = $redirectUris
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

# Use REST API PATCH to update ONLY the container image.
# Update-AzContainerApp does a full update that resets custom domain bindings.
$appGetForImage = Invoke-AzRestMethod -Method GET -Path $appRestPath
$appInfoForImage = $appGetForImage.Content | ConvertFrom-Json

# Update the image in the existing template (preserves env vars, resources, scale, etc.)
foreach ($container in $appInfoForImage.properties.template.containers) {
    if ($container.name -eq $AppName) {
        $container.image = $imageRef
    }
}

$imagePatchPayload = @{
    properties = @{
        template = @{
            containers = @($appInfoForImage.properties.template.containers)
        }
    }
} | ConvertTo-Json -Depth 10

Invoke-ContainerAppPatch -Path $appRestPath -Payload $imagePatchPayload -OperationLabel "update container image" | Out-Null
Write-Host "  Image updated to $imageRef" -ForegroundColor Green

# -------------------------------------------------------------------
# 7. Bind new custom domain with managed TLS certificate
# -------------------------------------------------------------------
if ($CustomDomain) {
    Write-Host "[7/7] Binding custom domain '$CustomDomain'..." -ForegroundColor Yellow

    $certName = ($CustomDomain -replace '[^a-zA-Z0-9-]', '-') + "-cert"
    $certPath = "/subscriptions/$subscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.App/managedEnvironments/${envName}/managedCertificates/${certName}?api-version=2024-03-01"

    # 7a. Add the custom domain to the Container App with binding DISABLED first.
    #     Azure requires the hostname to exist on the app BEFORE a managed cert can be created.
    Write-Host "  Adding domain to Container App (binding disabled)..." -ForegroundColor Gray

    $appGetResp2 = Invoke-AzRestMethod -Method GET -Path $appRestPath
    $appInfo2 = $appGetResp2.Content | ConvertFrom-Json
    $currentDomains = $appInfo2.properties.configuration.ingress.customDomains

    $domainsList = @()
    if ($currentDomains) {
        foreach ($d in $currentDomains) {
            if ($d.name -eq $CustomDomain) { continue }   # replace if already present
            $domainsList += @{
                name          = $d.name
                certificateId = $d.certificateId
                bindingType   = $d.bindingType
            }
        }
    }
    # Add with Disabled binding — no cert yet
    $domainsList += @{
        name        = $CustomDomain
        bindingType = "Disabled"
    }

    $patchPayload = @{
        properties = @{
            configuration = @{
                ingress = @{
                    customDomains = $domainsList
                }
            }
        }
    } | ConvertTo-Json -Depth 10

    Invoke-ContainerAppPatch -Path $appRestPath -Payload $patchPayload -OperationLabel "add custom domain (disabled)" | Out-Null
    Write-Host "  Domain added to Container App." -ForegroundColor Green

    # 7b. Create / reuse managed certificate (now the hostname exists on the app)
    $certCheck = Invoke-AzRestMethod -Method GET -Path $certPath
    if ($certCheck.StatusCode -eq 200) {
        $certInfo = $certCheck.Content | ConvertFrom-Json
        $certResourceId = $certInfo.id
        Write-Host "  Managed certificate already exists: $certName" -ForegroundColor Gray
    } else {
        Write-Host "  Creating managed certificate (this may take a few minutes)..." -ForegroundColor Gray
        $certPayload = @{
            location   = $Location
            properties = @{
                subjectName             = $CustomDomain
                domainControlValidation = "CNAME"
            }
        } | ConvertTo-Json -Depth 5

        $certCreateResp = Invoke-AzRestMethod -Method PUT -Path $certPath -Payload $certPayload
        if ($certCreateResp.StatusCode -notin @(200, 201, 202)) {
            throw "Failed to create managed certificate: $($certCreateResp.Content)"
        }
        $certInfo = $certCreateResp.Content | ConvertFrom-Json
        $certResourceId = $certInfo.id

        # Poll until provisioning succeeds (up to 5 minutes)
        $maxWait  = 300
        $elapsed  = 0
        $interval = 15
        do {
            Start-Sleep -Seconds $interval
            $elapsed += $interval
            $pollResp = Invoke-AzRestMethod -Method GET -Path $certPath
            $pollInfo = $pollResp.Content | ConvertFrom-Json
            $provState = $pollInfo.properties.provisioningState
            Write-Host "  Certificate status: $provState (${elapsed}s elapsed)..." -ForegroundColor Gray
        } while ($provState -notin @("Succeeded", "Failed", "Canceled") -and $elapsed -lt $maxWait)

        if ($provState -ne "Succeeded") {
            throw "Managed certificate provisioning failed with status: $provState"
        }
        Write-Host "  Managed certificate ready." -ForegroundColor Green
    }

    # 7c. Update the domain binding from Disabled to SniEnabled with the certificate
    Write-Host "  Enabling TLS binding..." -ForegroundColor Gray

    $appGetResp3 = Invoke-AzRestMethod -Method GET -Path $appRestPath
    $appInfo3 = $appGetResp3.Content | ConvertFrom-Json
    $currentDomains3 = $appInfo3.properties.configuration.ingress.customDomains

    $domainsList2 = @()
    if ($currentDomains3) {
        foreach ($d in $currentDomains3) {
            if ($d.name -eq $CustomDomain) {
                $domainsList2 += @{
                    name          = $CustomDomain
                    certificateId = $certResourceId
                    bindingType   = "SniEnabled"
                }
            } else {
                $domainsList2 += @{
                    name          = $d.name
                    certificateId = $d.certificateId
                    bindingType   = $d.bindingType
                }
            }
        }
    }

    $patchPayload2 = @{
        properties = @{
            configuration = @{
                ingress = @{
                    customDomains = $domainsList2
                }
            }
        }
    } | ConvertTo-Json -Depth 10

    Invoke-ContainerAppPatch -Path $appRestPath -Payload $patchPayload2 -OperationLabel "enable TLS binding" | Out-Null
    Write-Host "  Custom domain '$CustomDomain' bound with managed TLS certificate." -ForegroundColor Green
}

# -------------------------------------------------------------------
# Done!
# -------------------------------------------------------------------
Write-Host ""
Write-Host "=====================================" -ForegroundColor Green
Write-Host "  Deployment complete!" -ForegroundColor Green
Write-Host "=====================================" -ForegroundColor Green
Write-Host ""
Write-Host "  URLs:" -ForegroundColor White
Write-Host "    https://$fqdn" -ForegroundColor White

# Collect all active custom domains (existing + newly added)
$activeCustomDomains = @($existingDomainNames)
if ($CustomDomain -and ($activeCustomDomains -notcontains $CustomDomain)) {
    $activeCustomDomains += $CustomDomain
}
foreach ($d in $activeCustomDomains) {
    Write-Host "    https://$d  (custom)" -ForegroundColor White
}

Write-Host ""
Write-Host "  All users in your Entra ID tenant can sign in via any URL above." -ForegroundColor Gray

if ($activeCustomDomains.Count -eq 0) {
    Write-Host ""
    Write-Host "  ── Custom Domain ──" -ForegroundColor Cyan
    Write-Host "  To use a custom domain, create these DNS records:" -ForegroundColor Gray
    Write-Host ""
    Write-Host "    Type   Host                              Value" -ForegroundColor White
    Write-Host "    -----  --------------------------------  ----------------------------------------" -ForegroundColor Gray
    Write-Host "    CNAME  <your-domain>                     $fqdn" -ForegroundColor White
    Write-Host "    TXT    asuid.<your-domain>               $verificationId" -ForegroundColor White
    Write-Host ""
    Write-Host "  Then re-run:" -ForegroundColor Gray
    Write-Host "    .\deploy.ps1 -CustomDomain <your-domain>" -ForegroundColor White
}
# -------------------------------------------------------------------
# Clean up old app registration secrets (optional)
# -------------------------------------------------------------------
if ($CleanOldSecrets) {
    Write-Host ""
    Write-Host "Cleaning old client secrets (keeping KeyId $($credential.KeyId))..." -ForegroundColor Yellow
    $allCreds = Get-AzADAppCredential -ObjectId $objectId
    $removed = 0
    foreach ($c in $allCreds) {
        if ($c.KeyId -ne $credential.KeyId) {
            Remove-AzADAppCredential -ObjectId $objectId -KeyId $c.KeyId
            Write-Host "  Removed KeyId: $($c.KeyId)  (expired: $($c.EndDateTime))" -ForegroundColor Gray
            $removed++
        }
    }
    if ($removed -eq 0) {
        Write-Host "  No old secrets to remove." -ForegroundColor Gray
    } else {
        Write-Host "  Removed $removed old secret(s)." -ForegroundColor Green
    }
}
Write-Host ""
