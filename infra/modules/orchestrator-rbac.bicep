// Least-privilege RBAC for the DR orchestrator.
//
// It needs to be able to:
//   * read/scale the app services in both regions  -> Website Contributor (scoped)
//   * read + promote the SQL failover group        -> SQL Failover Group Contributor
//   * read + flip AFD origins                      -> CDN Endpoint Contributor
//
// It explicitly does NOT get Contributor at the subscription or resource-group
// level. A DR tool that can do anything is a DR tool that can cause the outage.

targetScope = 'resourceGroup'

param principalId string
param primaryResourceGroup string
param secondaryResourceGroup string
param failoverGroupName string

// ---- App Service: scale the standby up during failover -------------------
// Website Contributor at the primary RG (the orchestrator must read app health
// and change the plan instance count in both regions).
resource websiteContributorPrimary 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(principalId, primaryResourceGroup, 'website-contributor')
  scope: resourceGroup(primaryResourceGroup)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'de139f84-1756-47ae-9be6-808fbbe84772')
    principalId: principalId
    principalType: 'ServicePrincipal'
    description: 'DR orchestrator: scale + restart app services in primary'
  }
}

resource websiteContributorSecondary 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(principalId, secondaryResourceGroup, 'website-contributor')
  scope: resourceGroup(secondaryResourceGroup)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'de139f84-1756-47ae-9be6-808fbbe84772')
    principalId: principalId
    principalType: 'ServicePrincipal'
    description: 'DR orchestrator: scale + restart app services in secondary'
  }
}

// ---- SQL: promote the failover group ------------------------------------
// "SQL Failover Group Contributor" is not a built-in role; the narrowest
// built-in that permits failover is SQL DB Contributor scoped to the server.
resource sqlContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(principalId, primaryResourceGroup, 'sql-db-contributor')
  scope: resourceGroup(primaryResourceGroup)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '9b7fa17d-e63e-47b0-bb0a-15c516ac86ec')
    principalId: principalId
    principalType: 'ServicePrincipal'
    description: 'DR orchestrator: read and fail over the SQL failover group'
  }
}

// ---- Front Door: flip origin priorities --------------------------------
// CDN Endpoint Contributor at the global RG scope is granted in main.bicep's
// global module; documented here so the permission set is in one place:
//   * Microsoft.Cdn/profiles/originGroups/origins/read + write
//   * Microsoft.Cdn/profiles/afdEndpoints/read
//
// Read-only monitoring in both regions for evidence gathering:
resource readerBoth 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(principalId, secondaryResourceGroup, 'monitoring-reader')
  scope: resourceGroup(secondaryResourceGroup)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '43d0d8ad-25c7-4714-9337-8ba259a9fe05')
    principalId: principalId
    principalType: 'ServicePrincipal'
    description: 'DR orchestrator: read health metrics for evidence'
  }
}

output grantedRoles array = [
  'Website Contributor @ primary RG'
  'Website Contributor @ secondary RG'
  'SQL DB Contributor @ primary RG'
  'Monitoring Reader @ secondary RG'
  'CDN Endpoint Contributor @ global RG (granted in global module)'
]
