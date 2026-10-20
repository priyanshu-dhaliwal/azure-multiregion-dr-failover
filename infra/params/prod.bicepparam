using './main.bicep'

env = 'prod'
primaryLocation = 'centralindia'
secondaryLocation = 'southindia'
orchestratorLocation = 'westeurope'
suffix = 'dr01'
appServiceSku = 'P1v3'
primaryInstances = 3
standbyInstances = 1
sqlAdminLogin = 'sqladmin'
sqlAdminPassword = 'REPLACE_ME_OR_USE_KEY_VAULT'
