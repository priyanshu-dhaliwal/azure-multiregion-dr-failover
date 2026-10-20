// DR orchestrator: Function App (Flex Consumption, Python 3.11) in a THIRD
// region. Managed identity only — no connection strings, no keys.

targetScope = 'resourceGroup'

param namePrefix string
param location string
param primaryResourceGroup string
param secondaryResourceGroup string
param primaryAppName string
param secondaryAppName string
param frontDoorProfileName string
param cosmosEndpoint string
param cosmosDatabase string
param cosmosContainer string
param sqlServerName string
param failoverGroupName string
param tags object = {}

var storageName = replace('${namePrefix}orchestratorst', '-', '')

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
    allowSharedKeyAccess: false
  }
}

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: '${namePrefix}-orch-plan'
  location: location
  tags: tags
  sku: { name: 'FC1', tier: 'FlexConsumption' }
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: '${namePrefix}-orchestrator'
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.11'
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      appSettings: [
        { name: 'AzureWebJobsStorage__accountName', value: storage.name }
        { name: 'AzureWebJobsStorage__credential', value: 'managedidentity' }
        { name: 'FUNCTIONS_EXTENSION_VERSION', value: '~4' }
        { name: 'FUNCTIONS_WORKER_RUNTIME', value: 'python' }
        { name: 'PRIMARY_RG', value: primaryResourceGroup }
        { name: 'SECONDARY_RG', value: secondaryResourceGroup }
        { name: 'PRIMARY_APP', value: primaryAppName }
        { name: 'SECONDARY_APP', value: secondaryAppName }
        { name: 'AFD_PROFILE_ID', value: frontDoorProfileName }
        { name: 'COSMOS_ENDPOINT', value: cosmosEndpoint }
        { name: 'COSMOS_DATABASE', value: cosmosDatabase }
        { name: 'COSMOS_CONTAINER', value: cosmosContainer }
        { name: 'SQL_SERVER', value: sqlServerName }
        { name: 'FAILOVER_GROUP', value: failoverGroupName }
        // Tunables — deliberately conservative.
        { name: 'PROBE_FAILURES_TO_TRIGGER', value: '3' }
        { name: 'PROBE_INTERVAL_SECONDS', value: '20' }
        { name: 'FAILOVER_GRACE_SECONDS', value: '60' }
        { name: 'DRY_RUN', value: 'false' }
      ]
    }
  }
}

// Function App needs the storage account for its own bookkeeping.
resource storageBlobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(functionApp.id, storage.id, 'blob-data-owner')
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b')
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output functionAppName string = functionApp.name
output functionAppId string = functionApp.id
output principalId string = functionApp.identity.principalId
output defaultHostName string = functionApp.properties.defaultHostName
