// Global entry point: Azure Front Door Premium with priority routing.
//
//  priority 1 -> primary app  (100% of traffic in steady state)
//  priority 2 -> standby app  (0% until the primary fails its health probe)
//
// Failover at the edge is single-digit seconds. Traffic Manager would be
// 30-60s because it is DNS-based; that is the whole reason for AFD here.

targetScope = 'resourceGroup'

param namePrefix string
param primaryHostname string
param secondaryHostname string
param tags object = {}

resource afd 'Microsoft.Cdn/profiles@2023-05-01' = {
  name: '${namePrefix}-afd'
  location: 'global'
  sku: { name: 'Premium_AzureFrontDoor' }
  tags: tags
}

resource endpoint 'Microsoft.Cdn/profiles/afdEndpoints@2023-05-01' = {
  parent: afd
  name: '${namePrefix}-ep'
  location: 'global'
  properties: {
    enabledState: 'Enabled'
  }
}

resource originGroup 'Microsoft.Cdn/profiles/originGroups@2023-05-01' = {
  parent: afd
  name: 'app-origins'
  properties: {
    loadBalancingSettings: {
      sampleSize: 4
      successfulSamplesRequired: 3
      additionalLatencyInMilliseconds: 50
    }
    // Health probe: this is what triggers failover.
    healthProbeSettings: {
      probePath: '/health'
      probeRequestType: 'HEAD'
      probeProtocol: 'Https'
      probeIntervalInSeconds: 15
    }
    sessionAffinityState: 'Disabled'
  }
}

// Priority 1: primary. Lower number = preferred.
resource originPrimary 'Microsoft.Cdn/profiles/originGroups/origins@2023-05-01' = {
  parent: originGroup
  name: 'primary'
  properties: {
    hostName: primaryHostname
    httpPort: 80
    httpsPort: 443
    originHostHeader: primaryHostname
    priority: 1
    weight: 1000
    enabledState: 'Enabled'
    enforceCertificateNameCheck: true
  }
}

// Priority 2: standby. Only receives traffic when the primary is unhealthy.
resource originSecondary 'Microsoft.Cdn/profiles/originGroups/origins@2023-05-01' = {
  parent: originGroup
  name: 'secondary'
  properties: {
    hostName: secondaryHostname
    httpPort: 80
    httpsPort: 443
    originHostHeader: secondaryHostname
    priority: 2
    weight: 1000
    enabledState: 'Enabled'
    enforceCertificateNameCheck: true
  }
}

resource route 'Microsoft.Cdn/profiles/afdEndpoints/routes@2023-05-01' = {
  parent: endpoint
  name: 'default'
  properties: {
    originGroup: { id: originGroup.id }
    forwardingProtocol: 'HttpsOnly'
    httpsRedirect: 'Enabled'
    linkToDefaultDomain: 'Enabled'
    patternsToMatch: ['/*']
    cacheConfiguration: {
      queryStringCachingBehavior: 'IgnoreQueryString'
      compressionSettings: {
        isCompressionEnabled: true
        contentTypesToCompress: ['application/json', 'text/html', 'text/css', 'application/javascript']
      }
    }
  }
}

output profileId string = afd.id
output profileName string = afd.name
output endpointHostname string = endpoint.properties.hostName
output originGroupName string = originGroup.name
