param(
  [string]$BaseUrl = "http://127.0.0.1:8787",
  [string]$ConversationId = "integration-test-conversation",
  [string]$UserId = "integration-test-user",
  [int]$TimeoutSeconds = 60
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$capabilityPath = Join-Path $root "capabilities\_integration-atomic-approval-test.json"
$atomicPath = Join-Path $root "atomics\_integration-approval-notify.json"
$trigger = "integration-atomic-approval-test"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Invoke-JsonPost {
  param(
    [string]$Uri,
    [object]$Body
  )
  Invoke-RestMethod -Method POST -Uri $Uri -ContentType "application/json; charset=utf-8" -Body ($Body | ConvertTo-Json -Depth 20 -Compress)
}

function Wait-Until {
  param(
    [scriptblock]$Probe,
    [string]$Description
  )
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    $value = & $Probe
    if ($value) {
      return $value
    }
    Start-Sleep -Seconds 2
  } while ((Get-Date) -lt $deadline)
  throw "Timed out waiting for $Description"
}

try {
  [System.IO.File]::WriteAllText($atomicPath, @"
{
  "name": "integration.approval.notify",
  "label": "Integration approval notify",
  "description": "Temporary integration-test atomic. Requires approval before sending a notification.",
  "type": "notify",
  "risk": "high",
  "requires_approval": true
}
"@, $utf8NoBom)

  [System.IO.File]::WriteAllText($capabilityPath, @"
{
  "name": "integration-atomic-approval-test",
  "label": "Integration atomic approval test",
  "intent": "integration_atomic_approval_test",
  "aliases": ["integration_atomic_approval_test"],
  "triggers": ["$trigger"],
  "created_message": "Created integration atomic approval test workflow",
  "input_defaults": {},
  "stages": [
    {
      "name": "approval_notify",
      "label": "Approval notify",
      "executor": {
        "type": "atomic",
        "name": "integration.approval.notify",
        "input": {
          "message": "Integration approved workflow {workflow_id}"
        }
      }
    }
  ]
}
"@, $utf8NoBom)

  Write-Host "Reload registries..."
  Invoke-RestMethod -Method POST -Uri "$BaseUrl/api/admin/reload-capabilities" | Out-Null

  Write-Host "Create workflow from /api/messages..."
  $created = Invoke-JsonPost -Uri "$BaseUrl/api/messages" -Body @{
    conversation_id = $ConversationId
    user_id = $UserId
    text = "$trigger please run"
  }
  $created | ConvertTo-Json -Depth 10

  $workflowId = [regex]::Match($created.reply, "wf_[a-f0-9]+").Value
  if (-not $workflowId) {
    throw "Failed to parse workflow id from reply: $($created.reply)"
  }

  Write-Host "Wait for atomic approval request..."
  $approvalId = Wait-Until -Description "approval notification" -Probe {
    $notes = Invoke-RestMethod -Method GET -Uri "$BaseUrl/api/notifications?workflow_id=$workflowId&delivery_status=all"
    $messages = @($notes.items | ForEach-Object { $_.message })
    $match = [regex]::Match(($messages -join "`n"), "appr_[a-f0-9]+")
    if ($match.Success) {
      return $match.Value
    }
    return $null
  }
  Write-Host "Approval id: $approvalId"

  Write-Host "Approve through /api/messages..."
  $approved = Invoke-JsonPost -Uri "$BaseUrl/api/messages" -Body @{
    conversation_id = $ConversationId
    user_id = $UserId
    text = "/approve $approvalId"
  }
  $approved | ConvertTo-Json -Depth 10

  Write-Host "Wait for workflow succeeded..."
  $workflow = Wait-Until -Description "workflow success" -Probe {
    $state = Invoke-RestMethod -Method GET -Uri "$BaseUrl/api/workflows/$workflowId"
    if ($state.workflow.status -eq "succeeded") {
      return $state
    }
    if ($state.workflow.status -in @("failed", "cancelled")) {
      throw "Workflow ended with status $($state.workflow.status): $($state.workflow.error)"
    }
    return $null
  }
  $workflow | ConvertTo-Json -Depth 20

  Write-Host "Verify approved notification was queued..."
  $finalNotes = Invoke-RestMethod -Method GET -Uri "$BaseUrl/api/notifications?workflow_id=$workflowId&delivery_status=all"
  $hasApprovedMessage = @($finalNotes.items | Where-Object { $_.message -like "*Integration approved workflow $workflowId*" }).Count -gt 0
  if (-not $hasApprovedMessage) {
    throw "Approved atomic notification was not found for $workflowId"
  }

  Write-Host "Integration entrypoint test passed: $workflowId"
}
finally {
  Remove-Item -LiteralPath $capabilityPath -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $atomicPath -Force -ErrorAction SilentlyContinue
  try {
    Invoke-RestMethod -Method POST -Uri "$BaseUrl/api/admin/reload-capabilities" | Out-Null
  } catch {
    Write-Warning "Cleanup reload failed: $($_.Exception.Message)"
  }
}
