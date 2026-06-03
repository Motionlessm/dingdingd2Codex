$ErrorActionPreference = "Stop"

$base = "http://127.0.0.1:8787"

Write-Host "Health:"
Invoke-RestMethod -Method GET -Uri "$base/health" | ConvertTo-Json -Depth 5

Write-Host "`nCreate logs workflow:"
$created = Invoke-RestMethod -Method POST -Uri "$base/api/messages" -ContentType "application/json; charset=utf-8" -Body (@{
  conversation_id = "ding_group_001:user_001"
  user_id = "user_001"
  text = "查日志 payment error"
} | ConvertTo-Json -Compress)
$created | ConvertTo-Json -Depth 5

$workflowId = [regex]::Match($created.reply, "wf_[a-f0-9]+").Value
if (-not $workflowId) {
  throw "Failed to parse workflow id"
}

Write-Host "`nWait workflow..."
Start-Sleep -Seconds 10

Write-Host "`nStatus:"
Invoke-RestMethod -Method GET -Uri "$base/api/workflows/$workflowId" | ConvertTo-Json -Depth 10

Write-Host "`nNotifications:"
Invoke-RestMethod -Method GET -Uri "$base/api/notifications?workflow_id=$workflowId" | ConvertTo-Json -Depth 10
