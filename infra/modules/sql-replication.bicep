// SQL auto-failover group: primary server (centralindia) + geo-secondary
// (southindia). The group is the unit of failover — the app always connects to
// the group listener, never to a specific server. That is what makes failover
// possible without an app config change.

targetScope = 'resourceGroup'

param namePrefix string
param primaryServerName string
param secondaryServerId string
param databaseName string
param primaryResourceGroup string
param secondaryResourceGroup string

@description('Grace period before Azure auto-promotes. Shorter than default 1h.')
@minValue(1)
@maxValue(60)
param gracePeriodMinutes int = 15

var groupName = '${namePrefix}-fog'

resource failoverGroup 'Microsoft.Sql/servers/failoverGroups@2023-08-01-preview' = {
  name: groupName
  parent: resourceId(primaryResourceGroup, 'Microsoft.Sql/servers', primaryServerName)
  properties: {
    // The listener endpoint clients connect to. It follows the current primary.
    readWriteEndpoint: {
      failoverPolicy: 'Automatic'
      failoverWithDataLossGracePeriodMinutes: gracePeriodMinutes
    }
    readOnlyEndpoint: {
      failoverPolicy: 'Disabled'
    }
    // Partner server id is the SECONDARY server's resource id.
    partnerServers: [
      {
        id: secondaryServerId
      }
    ]
    databases: [
      resourceId(primaryResourceGroup, 'Microsoft.Sql/servers/databases', primaryServerName, databaseName)
    ]
  }
}

output failoverGroupName string = failoverGroup.name
// The listener is what the app connects to; it resolves to the current primary.
output readWriteListener string = '${groupName}.database.windows.net'
output groupId string = failoverGroup.id
