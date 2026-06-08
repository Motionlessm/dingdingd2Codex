# Claude Project Context

This document is intended to be pasted into a new Claude/Codex session so the agent can work on this project without guessing the local architecture.

## Project Location

```text
D:\dingdingd2Codex
```

This is a local DingTalk Agent framework. DingTalk is the chat entrypoint, the planner interprets user intent, the workflow executor owns long-running state, and controlled executors perform actual database/log/API/script operations.

Do not include secrets in generated code, prompts, capability files, or docs.

## Current Runtime

Default local service:

```text
http://127.0.0.1:8787
```

Useful endpoints:

```text
GET  /health
GET  /dashboard
POST /api/messages
GET  /api/capabilities
POST /api/admin/reload-capabilities
GET  /api/workflows
GET  /api/workflows/{workflow_id}
GET  /api/notifications
GET  /api/notifications?delivery_status=pending|sent|failed|all
POST /api/notifications/{notification_id}/delivery
```

The public entrypoint is always `/api/messages`. Test from that entrypoint, not by calling internal Python functions directly.

Example:

```powershell
Invoke-RestMethod -Method POST -Uri "http://127.0.0.1:8787/api/messages" `
  -ContentType "application/json; charset=utf-8" `
  -Body '{"conversation_id":"local-test","user_id":"u1","text":"鏌ユ棩蹇?payment error"}'
```

## Important Files

```text
app.py                         Main service, planner, workflow executor, gateway, API, dashboard
capabilities/*.json            Dynamic capability registry
atomics/*.json                 Reusable atomic ability registry
executors/*                    Controlled command executors
data/agent.db                  SQLite runtime state
data/artifacts/*.json          Stored workflow payloads
run.ps1                        Start local service
smoke-test.ps1                 Entry-level smoke test
```

Current built-in example capability:

```text
capabilities/logs-search.json
```

There should be no hardcoded business workflow in `app.py` unless the framework itself needs a reusable generic feature.

## Capability Registry Contract

New business capabilities should be added as JSON files under:

```text
D:\dingdingd2Codex\capabilities
```

Shape:

```json
{
  "name": "capability-name",
  "label": "User readable label",
  "intent": "capability_intent",
  "aliases": ["capability_alias"],
  "triggers": ["natural language trigger"],
  "created_message": "Task created",
  "input_defaults": {},
  "stages": [
    {
      "name": "stage_name",
      "label": "Stage label",
      "executor": {
        "type": "command",
        "command": ["python", "executors/example.py"],
        "timeout_seconds": 60,
        "poll_interval_seconds": 300,
        "max_wait_seconds": 21600
      }
    }
  ]
}
```

After changing `capabilities/*.json`, reload without restarting:

```powershell
Invoke-RestMethod -Method POST -Uri "http://127.0.0.1:8787/api/admin/reload-capabilities"
Invoke-RestMethod -Method GET -Uri "http://127.0.0.1:8787/api/capabilities"
```

## Atomic Capability Layer

Reusable atomic abilities are registered under:

```text
D:\dingdingd2Codex\atomics
```

They are synced into SQLite table `atomic_capabilities`; every atomic call is audited in `atomic_invocations`.

Useful endpoints:

```text
GET  /api/atomics
POST /api/admin/reload-atomics
```

`POST /api/admin/reload-capabilities` also reloads atomics.

Atomic file shape:

```json
{
  "name": "dingtalk.notify",
  "label": "閽夐拤閫氱煡",
  "description": "Send a workflow notification",
  "type": "notify",
  "risk": "low",
  "requires_approval": false
}
```

Workflow stages can call an atomic ability:

```json
{
  "name": "notice",
  "label": "Send notice",
  "executor": {
    "type": "atomic",
    "name": "dingtalk.notify",
    "input": {
      "message": "Workflow {workflow_id} stage {stage} completed"
    }
  }
}
```

Supported atomic types currently:

```text
notify      Send through framework NotificationRouter
command     Run a registered command executor
```

`dingtalk.notify` writes a row to `notifications` with `status=pending`. `NotificationRouter` then tries to deliver the message through `cc-connect send --session <conversation_id>` and marks the row `sent` on success. Pending rows remain available for the cc-connect orchestrator agent polling fallback, which calls `POST /api/notifications/{id}/delivery` after it hands the message to cc-connect's platform event stream. Current statuses are `pending`, `sent`, and `failed`; `sent` means accepted by cc-connect for DingTalk delivery, not a DingTalk server-side receipt.

Use atomics for reusable internal abilities such as DingTalk notification, controlled MySQL operations, log lookup, HTTP calls, or approval helpers. Business workflow executors should not reimplement these concerns when an atomic ability exists.

Business workflows should prefer `executor.type=atomic` for reusable or sensitive operations. A command executor can still exist, but it should usually be registered behind an atomic so the framework gets approval, audit, reload, and reuse through `atomic_invocations`.

Risky or write-capable atomics should set:

```json
{
  "requires_approval": true,
  "risk": "high"
}
```

Atomic approval is bound to the workflow id, stage, atomic name, and the hash of the atomic input payload. After `/approve <approval_id>`, the same stage is retried and only that approved atomic input is allowed to execute. If the input changes, a new approval is required.

## Command Executor Contract

Command executors live under:

```text
D:\dingdingd2Codex\executors
```

They receive JSON on stdin:

```json
{
  "workflow_id": "wf_xxx",
  "skill": "capability-name",
  "stage": "stage_name",
  "payload": {},
  "executor": {},
  "stage_state": {}
}
```

They must print one JSON object to stdout:

```json
{
  "status": "succeeded",
  "job_id": "job_xxx",
  "submitted_count": 1,
  "pending_count": 0,
  "success_count": 1,
  "failed_count": 0,
  "message": "short notification text"
}
```

Allowed statuses:

```text
succeeded   Advance to next stage
running     Keep current stage and poll it later
failed      Trigger failure/recovery handling
```

## Long-Running Work

Long-running tasks must be non-blocking.

Executor scripts must not run long `while` loops or sleep for hours. Each executor invocation should do one atomic action or one status check, then exit.

If a stage is still waiting for an external system, return:

```json
{
  "status": "running",
  "pending_count": 42,
  "success_count": 58,
  "failed_count": 0,
  "next_check_seconds": 300,
  "max_wait_seconds": 21600,
  "message": "Still processing; will check again later"
}
```

The framework persists and re-invokes the stage later using:

```text
workflow_stages.next_check_at
workflow_stages.timeout_at
workflow_stages.attempt_count
workflow_stages.result_json
workflow_stage_runs
```

`timeout_seconds` in the executor config is only the maximum runtime of one executor process. It is not the business wait budget. Use `max_wait_seconds` for business timeout.

## Runtime Tables

SQLite runtime tables are created/migrated by `Store.init` in `app.py`.

Important tables:

```text
workflows
workflow_stages
workflow_stage_runs
atomic_capabilities
atomic_invocations
notifications
pending_confirmations
approvals
```

`workflow_stage_runs` is an audit table: every executor invocation creates a run record with input/output/error.
`atomic_invocations` is an audit table: every atomic ability call creates a run record with input/output/error.
`notifications` is a delivery queue with status fields: `status`, `delivery_attempts`, `last_error`, `delivered_at`, and `updated_at`.

## Planner And Model

The planner can use an OpenAI-compatible API when these environment variables are set:

```text
PLANNER_PROVIDER=api
LLM_API_BASE_URL=...
LLM_API_KEY=...
LLM_MODEL=...
PLANNER_TIMEOUT_SECONDS=...
```

Do not print or commit API keys.

The planner sees registered capabilities via the capability registry and should choose an `intent` matching the selected capability.

## Development Rules

- Prefer adding `capabilities/*.json`, `atomics/*.json`, and `executors/*`.
- Do not place DSNs, tokens, or secrets in capability JSON.
- Use environment variables or private local config for secrets.
- AI/planner may infer parameters but must not directly run SQL, shell, HTTP, or credentialed calls.
- Executor code must validate allowed operations, tables/resources, stages, parameters, and limits.
- Database writes and risky production actions require approval unless explicitly configured otherwise.
- SQL must be parameterized.
- If a business completion rule is missing, stop at a plan or create only a dry-run stub.
- If only capability/executor files changed, reload capabilities. Restart service only after changing `app.py` or runtime framework behavior.

## Validation

Compile changed Python:

```powershell
python -m py_compile D:\dingdingd2Codex\app.py
```

Compile changed executors:

```powershell
python -m py_compile D:\dingdingd2Codex\executors\<executor>.py
```

Run smoke test:

```powershell
cd D:\dingdingd2Codex
.\smoke-test.ps1
```

Run entrypoint integration test for registry reload, workflow creation, atomic approval, `/approve`, workflow resume, and notification queue:

```powershell
cd D:\dingdingd2Codex
.\scripts\integration-entrypoint-test.ps1
```

Check dashboard:

```text
http://127.0.0.1:8787/dashboard
```

