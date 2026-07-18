Write-Host "🚀 Starting Microsoft Azure Deployment Process..."

$LOCATION = "eastasia" 
# Appended region name to completely bypass the locked 'centralindia' group
$RESOURCE_GROUP = "aivar-guardrail-eastasia-rg"
$ACR_NAME = "aivarguardrailacr$((Get-Random -Minimum 10000 -Maximum 99999))" 
$APP_NAME = "aivar-action-guardrail"

Write-Host "1. Registering required Azure Providers..."
az provider register --namespace Microsoft.App --wait
az provider register --namespace Microsoft.ContainerRegistry --wait

Write-Host "2. Creating a fresh Resource Group in $LOCATION..."
az group create --name $RESOURCE_GROUP --location $LOCATION

Write-Host "3. Creating Azure Container Registry in $LOCATION..."
az acr create --resource-group $RESOURCE_GROUP --name $ACR_NAME --sku Basic --location $LOCATION
    
Write-Host "4. Building and pushing Docker image to Azure..."
az acr build --registry $ACR_NAME --image guardrail-proxy:latest .

Write-Host "5. Deploying to Azure Container Apps..."
az containerapp up `
    --name $APP_NAME `
    --resource-group $RESOURCE_GROUP `
    --image "$ACR_NAME.azurecr.io/guardrail-proxy:latest" `
    --ingress external `
    --target-port 8000 `
    --env-vars PROXY_URL="http://localhost:8000"

Write-Host "✅ Deployment initiated!"
