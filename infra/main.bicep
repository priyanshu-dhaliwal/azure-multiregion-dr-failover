// Multi-region DR topology.
//
// Deploys: primary region stack, secondary region stack, SQL geo-failover group,
// global Front Door with priority routing, and an orchestrator in a third region.
//
//   az deployment sub create -l centralindia -f infra/main.bicep -p infra/params/prod.bicepparam

targetScope = 'subscription'

@description('Environment')
@allowed(['dev', 'prod'])
param env string = 'prod'

@description('Primary region (serves traffic in steady state)')
param primaryLocation string = 'centralindia'

@description('Secondary / DR region (warm standby)')
param secondaryLocation string = 'southindia'

@description('Orchestrator region — deliberately a THIRD region so it survives either outage')
param orchestratorLocation string = 'westeurope'

@description('Short unique suffix')
@minLength(3)
@maxLength(8)
param suffix string = 'dr01'

@description('App Service SKU')
param appServiceSku string = 'P1v3'

@description('SQL admin login (password supplied at deploy time or from Key Vault)')
param sqlAdminLogin string = 'sqladmin'
@secure()
param sqlAdminPassword string

@description('Instances in the primary app tier')
param primaryInstances int = 3

@description('Instances in the standby app tier (scale out during failover)')
param standbyInstances int = 1

param tags object = {
  project: 'multiregion-dr'
  owner: 'platform'
}

var rgPrimary = 'rg-${env}-dr-primary-${suffix}'
var rgSecondary = 'rg-${env}-dr-secondary-${suffix}'
var rgGlobal = 'rg-${env}-dr-global-${suffix}'
var prefix = '${env}-dr-${suffix}'

// ---- Resource groups -----------------------------------------------------
resource rgP 'Microsoft.Resources/resourceGroups@2023-07-01' = {
  name: rgPrimary
  location: primaryLocation
  tags: union(tags, { role: 'primary', region: primaryLocation })
}

resource rgS 'Microsoft.Resources/resourceGroups@2023-07-01' = {
  name: rgSecondary
  location: secondaryLocation
  tags: union(tags, { role: 'secondary', region: secondaryLocation })
}

resource rgG 'Microsoft.Resources/resourceGroups@2023-07-01' = {
  name: rgGlobal
  location: orchestratorLocation
  tags: union(tags, { role: 'global' })
}

// ---- Per-region stacks ---------------------------------------------------
module primary './modules/region.bicep' = {
  scope: rgP
  name: 'region-primary'
  params: {
    namePrefix: prefix
    location: primaryLocation
    role: 'primary'
    appServiceSku: appServiceSku
    instances: primaryInstances
    sqlAdminLogin: sqlAdminLogin
    sqlAdminPassword: sqlAdminPassword
    tags: union(tags, { role: 'primary' })
  }
}

module secondary './modules/region.bicep' = {
  scope: rgS
  name: 'region-secondary'
  params: {
    namePrefix: prefix
    location: secondaryLocation
    role: 'secondary'
    appServiceSku: appServiceSku
    instances: standbyInstances
    sqlAdminLogin: sqlAdminLogin
    sqlAdminPassword: sqlAdminPassword
    tags: union(tags, { role: 'secondary' })
  }
}

// ---- SQL geo-replication -------------------------------------------------
module sqlReplication './modules/sql-replication.bicep' = {
  scope: rgP
  name: 'sql-replication'
  params: {
    namePrefix: prefix
    primaryServerName: primary.outputs.sqlServerName
    secondaryServerId: secondary.outputs.sqlServerId
    databaseName: primary.outputs.databaseName
    primaryResourceGroup: rgPrimary
    secondaryResourceGroup: rgSecondary
    gracePeriodMinutes: 15
  }
  dependsOn: [secondary]
}

// ---- State store (state machine document) --------------------------------
module stateStore './modules/state-store.bicep' = {
  scope: rgG
  name: 'state-store'
  params: {
    namePrefix: prefix
    location: orchestratorLocation
    tags: tags
  }
}

// ---- Global Front Door ---------------------------------------------------
module frontDoor './modules/frontdoor.bicep' = {
  scope: rgG
  name: 'frontdoor'
  params: {
    namePrefix: prefix
    primaryHostname: primary.outputs.appHostname
    secondaryHostname: secondary.outputs.appHostname
    tags: tags
  }
}

// ---- Orchestrator (third region) ----------------------------------------
module orchestrator './modules/orchestrator.bicep' = {
  scope: rgG
  name: 'orchestrator'
  params: {
    namePrefix: prefix
    location: orchestratorLocation
    primaryResourceGroup: rgPrimary
    secondaryResourceGroup: rgSecondary
    primaryAppName: primary.outputs.appName
    secondaryAppName: secondary.outputs.appName
    frontDoorProfileName: frontDoor.outputs.profileId
    cosmosEndpoint: stateStore.outputs.accountEndpoint
    cosmosDatabase: stateStore.outputs.databaseName
    cosmosContainer: stateStore.outputs.containerName
    sqlServerName: primary.outputs.sqlServerName
    failoverGroupName: sqlReplication.outputs.failoverGroupName
    tags: tags
  }
}

// Give the orchestrator identity the rights it needs, scoped tightly.
module orchestratorRbac './modules/orchestrator-rbac.bicep' = {
  scope: rgP
  name: 'orchestrator-rbac'
  params: {
    principalId: orchestrator.outputs.principalId
    secondaryResourceGroup: rgSecondary
    primaryResourceGroup: rgPrimary
    failoverGroupName: sqlReplication.outputs.failoverGroupName
  }
}

output primaryHostname string = primary.outputs.appHostname
output secondaryHostname string = secondary.outputs.appHostname
output frontDoorEndpoint string = frontDoor.outputs.endpointHostname
output failoverGroupName string = sqlReplication.outputs.failoverGroupName
output orchestratorName string = orchestrator.outputs.functionAppName
output stateStoreEndpoint string = stateStore.outputs.accountEndpoint
