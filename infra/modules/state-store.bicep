// Cosmos DB state store for the failover state machine.
//
// One document per environment holds the current state (HEALTHY, FAILED_OVER,
// ...) plus the evidence trail. Optimistic concurrency via _etag means two
// orchestrator instances can never run a failover simultaneously.

targetScope = 'resourceGroup'

param namePrefix string
param location string
param tags object = {}

var accountName = replace('${namePrefix}-drstate', '-', '')

resource account 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' = {
  name: accountName
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    consistencyPolicy: {
      // Strong enough that the orchestrator never reads a stale state.
      defaultConsistencyLevel: 'Strong'
      maxIntervalInSeconds: 5
      maxStalenessPrefix: 100
    }
    locations: [
      { locationName: location, failoverPriority: 0, isZoneRedundant: true }
    ]
    enableFreeTier: false
    minimalTlsVersion: 'Tls12'
    disableLocalAuth: true   // AAD-only; the orchestrator uses its managed identity
  }
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' = {
  parent: account
  name: 'dr'
  properties: {
    resource: { id: 'dr' }
    options: { throughput: 400 }
  }
}

resource stateContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: 'state'
  properties: {
    resource: {
      id: 'state'
      partitionKey: { paths: ['/environment'], kind: 'Hash' }
      // State documents are small and long-lived; no TTL.
    }
    options: { throughput: 400 }
  }
}

resource auditContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: 'transitions'
  properties: {
    resource: {
      id: 'transitions'
      partitionKey: { paths: ['/environment'], kind: 'Hash' }
      defaultTtl: 31536000   // 1 year of transition history
    }
    options: { throughput: 400 }
  }
}

output accountEndpoint string = account.properties.documentEndpoint
output accountName string = account.name
output databaseName string = database.name
output containerName string = stateContainer.name
output auditContainerName string = auditContainer.name
