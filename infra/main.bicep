// ---------------------------------------------------------------------------
// TranslateDoc – Azure infrastructure
// Deploys: Translator, ACR, Container App, Managed Identity, Storage Account
// ---------------------------------------------------------------------------

@description('Base name used for all resources')
param appName string = 'translatedoc'

@description('Azure region')
param location string = resourceGroup().location

@description('Entra ID (Azure AD) app registration client ID for authentication')
@secure()
param entraClientId string

@description('Entra ID app registration client secret')
@secure()
param entraClientSecret string

@description('Container image (set after first build)')
param containerImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('Existing custom domains to preserve across deployments (JSON array of objects with name, certificateId, bindingType)')
param customDomains array = []

// ---------------------------------------------------------------------------
// Variables
// ---------------------------------------------------------------------------

var uniqueSuffix = uniqueString(resourceGroup().id)
var acrName = '${replace(appName, '-', '')}${uniqueSuffix}'
var translatorName = '${appName}-${uniqueSuffix}'
var openaiName = '${appName}-openai-${uniqueSuffix}'
var storageAccountName = 'sttranslate${uniqueSuffix}'
var envName = '${appName}-env'
var appInsightsName = '${appName}-insights'
var logAnalyticsName = '${appName}-logs'
var identityName = '${appName}-identity'

// ---------------------------------------------------------------------------
// User-Assigned Managed Identity
// ---------------------------------------------------------------------------

resource managedIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: identityName
  location: location
}

// ---------------------------------------------------------------------------
// Log Analytics Workspace
// ---------------------------------------------------------------------------

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2025-07-01' = {
  name: logAnalyticsName
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

// ---------------------------------------------------------------------------
// Application Insights
// ---------------------------------------------------------------------------

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

// ---------------------------------------------------------------------------
// Azure AI Translator (Cognitive Services)
// ---------------------------------------------------------------------------

resource translator 'Microsoft.CognitiveServices/accounts@2025-10-01-preview' = {
  name: translatorName
  location: location
  kind: 'TextTranslation'
  sku: { name: 'S1' }
  properties: {
    customSubDomainName: translatorName
    publicNetworkAccess: 'Enabled'
  }
  identity: {
    type: 'SystemAssigned'
  }
}

// Cognitive Services User role → Managed Identity
var cognitiveServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'

resource translatorRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(translator.id, managedIdentity.id, cognitiveServicesUserRoleId)
  scope: translator
  properties: {
    principalId: managedIdentity.properties.principalId
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      cognitiveServicesUserRoleId
    )
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------------------
// Azure OpenAI (for glossary-enhanced translation)
// ---------------------------------------------------------------------------

resource openai 'Microsoft.CognitiveServices/accounts@2025-10-01-preview' = {
  name: openaiName
  location: location
  kind: 'OpenAI'
  sku: { name: 'S0' }
  properties: {
    customSubDomainName: openaiName
    publicNetworkAccess: 'Enabled'
    storedCompletionsDisabled: true
  }
}

resource openaiDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-10-01-preview' = {
  parent: openai
  name: 'gpt-5.2-chat'
  sku: {
    name: 'GlobalStandard'
    capacity: 30
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-5.2-chat'
      version: '2026-02-10'
    }
  }
}

// Cognitive Services OpenAI User role → Managed Identity
var cognitiveServicesOpenAIUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource openaiRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(openai.id, managedIdentity.id, cognitiveServicesOpenAIUserRoleId)
  scope: openai
  properties: {
    principalId: managedIdentity.properties.principalId
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      cognitiveServicesOpenAIUserRoleId
    )
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------------------
// Azure Container Registry
// ---------------------------------------------------------------------------

resource acr 'Microsoft.ContainerRegistry/registries@2025-11-01' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

// AcrPull role → Managed Identity
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource acrPullRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, managedIdentity.id, acrPullRoleId)
  scope: acr
  properties: {
    principalId: managedIdentity.properties.principalId
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      acrPullRoleId
    )
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------------------
// Azure Storage Account (for Batch Document Translation)
// ---------------------------------------------------------------------------

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storageAccount
  name: 'default'
}

resource sourceContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'source'
}

resource targetContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'target'
}

// Safety-net: delete blobs older than 1 day (app cleans up immediately after translation)
resource lifecyclePolicy 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: storageAccount
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'cleanup-translation-blobs'
          enabled: true
          type: 'Lifecycle'
          definition: {
            actions: {
              baseBlob: {
                delete: {
                  daysAfterModificationGreaterThan: 1
                }
              }
            }
            filters: {
              blobTypes: [ 'blockBlob' ]
            }
          }
        }
      ]
    }
  }
}

// Storage Blob Data Contributor → Managed Identity (our app uploads/downloads)
var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

resource storageRoleApp 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, managedIdentity.id, storageBlobDataContributorRoleId)
  scope: storageAccount
  properties: {
    principalId: managedIdentity.properties.principalId
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      storageBlobDataContributorRoleId
    )
    principalType: 'ServicePrincipal'
  }
}

// Storage Blob Data Contributor → Translator (batch API accesses blobs)
resource storageRoleTranslator 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, translator.id, storageBlobDataContributorRoleId)
  scope: storageAccount
  properties: {
    principalId: translator.identity.principalId
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      storageBlobDataContributorRoleId
    )
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------------------
// Container App Environment
// ---------------------------------------------------------------------------

resource containerAppEnv 'Microsoft.App/managedEnvironments@2025-07-01' = {
  name: envName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Container App
// ---------------------------------------------------------------------------

resource containerApp 'Microsoft.App/containerApps@2025-07-01' = {
  name: appName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppEnv.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'http'
        allowInsecure: false
        customDomains: customDomains
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: managedIdentity.id
        }
      ]
      secrets: [
        {
          name: 'microsoft-provider-authentication-secret'
          value: entraClientSecret
        }
      ]
    }
    template: {
      containers: [
        {
          name: appName
          image: containerImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'AZURE_TRANSLATOR_ENDPOINT', value: 'https://${translatorName}.cognitiveservices.azure.com' }
            { name: 'AZURE_TRANSLATOR_REGION', value: location }
            { name: 'USE_MANAGED_IDENTITY', value: 'true' }
            { name: 'AZURE_CLIENT_ID', value: managedIdentity.properties.clientId }
            { name: 'AZURE_STORAGE_ACCOUNT_NAME', value: storageAccount.name }
            { name: 'AZURE_OPENAI_ENDPOINT', value: 'https://${openaiName}.openai.azure.com' }
            { name: 'AZURE_OPENAI_DEPLOYMENT', value: 'gpt-5.2-chat' }
          ]
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 1
        cooldownPeriod: 1800
        rules: [
          {
            name: 'http-rule'
            http: { metadata: { concurrentRequests: '50' } }
          }
        ]
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Container App – Entra ID Authentication
// ---------------------------------------------------------------------------

resource authConfig 'Microsoft.App/containerApps/authConfigs@2025-07-01' = {
  parent: containerApp
  name: 'current'
  properties: {
    platform: { enabled: true }
    globalValidation: {
      unauthenticatedClientAction: 'RedirectToLoginPage'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: entraClientId
          clientSecretSettingName: 'microsoft-provider-authentication-secret'
          openIdIssuer: 'https://sts.windows.net/${tenant().tenantId}/v2.0'
        }
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer
output containerAppName string = containerApp.name
output containerAppFqdn string = containerApp.properties.configuration.ingress.fqdn
output containerAppEnvName string = containerAppEnv.name
output translatorName string = translator.name
output translatorEndpoint string = 'https://${translatorName}.cognitiveservices.azure.com'
output openaiName string = openai.name
output openaiEndpoint string = 'https://${openaiName}.openai.azure.com'
output managedIdentityClientId string = managedIdentity.properties.clientId
output storageAccountName string = storageAccount.name
