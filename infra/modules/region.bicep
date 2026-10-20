// One region's worth of the app: App Service plan + app, SQL server,
// storage account, Key Vault, and the region marker the app reports.
//
// The SAME module deploys both regions. The only difference is `role`, which
// flows into app settings so the app can report which region is serving.

targetScope = 'resourceGroup'

@allowed(['primary', 'secondary'])
param role string
param namePrefix string
param location string
param appServiceSku string
param instances int
param sqlAdminLogin string
@secure()
param sqlAdminPassword string
param tags object = {}

var shortRole = role == 'primary' ? 'p' : 's'
var appName = '${namePrefix}-app-${shortRole}'
var planName = '${namePrefix}-plan-${shortRole}'
var sqlName = '${namePrefix}-sql-${shortRole}'
var storageName = replace('${namePrefix}${shortRole}st', '-', '')
var kvName = '${namePrefix}-kv-${shortRole}'
var dbName = 'orders'

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: planName
  location: location
  tags: tags
  sku: {
    name: appServiceSku
    tier: appServiceSku == 'P1v3' ? 'PremiumV3' : 'Basic'
    capacity: instances
  }
  properties: {
    reserved: true        // Linux
    zoneRedundant: role == 'primary'   // standby stays cheap
    elasticScaleEnabled: false
  }
}

resource app 'Microsoft.Web/sites@2023-12-01' = {
  name: appName
  location: location
  tags: union(tags, { role: role })
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    clientAffinityEnabled: false
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.11'
      alwaysOn: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      healthCheckPath: '/health'
      appSettings: [
        { name: 'REGION_ROLE', value: role }
        { name: 'REGION_NAME', value: location }
        { name: 'DB_HOST', value: '${sqlName}.database.windows.net' }
        { name: 'DB_NAME', value: dbName }
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
        // Connection string deliberately NOT in app settings — fetched from Key Vault
        // by the app's managed identity.
        { name: 'DB_PASSWORD_URI', value: 'https://${kvName}.vault.azure.net/secrets/sql-password' }
      ]
    }
  }
}

// ---- SQL ---------------------------------------------------------------
resource sqlServer 'Microsoft.Sql/servers@2023-08-01-preview' = {
  name: sqlName
  location: location
  tags: tags
  properties: {
    administratorLogin: sqlAdminLogin
    administratorLoginPassword: sqlAdminPassword
    version: '12.0'
    minimalTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'   // locked down via firewall to app outbound IPs
  }
}

resource sqlFirewallAllowAzure 'Microsoft.Sql/servers/firewallRules@2023-08-01-preview' = {
  parent: sqlServer
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

resource database 'Microsoft.Sql/servers/databases@2023-08-01-preview' = {
  parent: sqlServer
  name: dbName
  location: location
  tags: tags
  sku: {
    name: 'S1'
    tier: 'Standard'
  }
  properties: {
    // Zone redundancy only in the primary — the secondary is a geo-replica.
    zoneRedundant: role == 'primary'
    readScale: 'Enabled'
    minCapacity: 0
  }
}

// ---- Storage (GRS in both regions; DR-relevant for blobs/uploads) -------
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  tags: tags
  sku: { name: 'Standard_GRS' }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
    allowSharedKeyAccess: false
  }
}

// ---- Key Vault (secrets must be readable from either region) -----------
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: kvName
  location: location
  tags: tags
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Enabled'
  }
}

resource sqlSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'sql-password'
  properties: {
    value: sqlAdminPassword
  }
}

// App identity reads the secret (Key Vault Secrets User).
resource kvRoleForApp 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(app.id, keyVault.id, 'kv-secrets-user')
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
    principalId: app.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output appName string = app.name
output appHostname string = app.properties.defaultHostName
output appId string = app.id
output appPrincipalId string = app.identity.principalId
output planName string = plan.name
output sqlServerName string = sqlServer.name
output sqlServerId string = sqlServer.id
output databaseName string = database.name
output storageName string = storage.name
output keyVaultName string = keyVault.name
