using './main.bicep'

env = 'dev'
primaryLocation = 'centralindia'
secondaryLocation = 'southindia'
orchestratorLocation = 'westeurope'
suffix = 'dev01'
appServiceSku = 'B1'
primaryInstances = 1
standbyInstances = 1
sqlAdminLogin = 'sqladmin'
sqlAdminPassword = 'REPLACE_ME_OR_USE_KEY_VAULT'
