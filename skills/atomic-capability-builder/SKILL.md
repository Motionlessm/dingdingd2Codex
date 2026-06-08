---
name: atomic-capability-builder
description: Design and integrate atomic capabilities for the local DingTalk Codex Agent framework from natural-language requirements.
---

# Atomic Capability Builder

Use this skill when the user describes a DingTalk workflow, internal tool, reusable atomic ability, long-running job, approval flow, log query, database operation, HTTP integration, or AI-generated execution plan.

## Project

Project root:

```text
D:\dingdingd2Codex
```

Important paths:

- Main service: `D:\dingdingd2Codex\app.py`
- Workflow registry: `D:\dingdingd2Codex\capabilities\*.json`
- Atomic registry: `D:\dingdingd2Codex\atomics\*.json`
- Controlled executors: `D:\dingdingd2Codex\executors\*`
- Project context for new sessions: `D:\dingdingd2Codex\CLAUDE_PROJECT_CONTEXT.md`
- Builder script: `D:\dingdingd2Codex\scripts\capability_builder.py`
- Dashboard: `http://127.0.0.1:8787/dashboard`
- Public entrypoint: `POST http://127.0.0.1:8787/api/messages`

Do not put secrets, DSNs, tokens, private endpoints, or raw credentials in skills, prompts, capability files, generated code, or docs. Use environment variables or local private config.

## Mode Rules

Default to plan-only when the user asks to plan, design, analyze, review, inspect, or says they only want a plan.

In plan-only mode:

- Do not edit files.
- Do not create executors.
- Do not restart services.
- Do not run mutating tests.
- Return the parsed capability spec, workflow stages, atomics, polling policy, notification policy, approval policy, recovery policy, assumptions, and blocking questions.

Implementation mode applies only when the user explicitly asks to implement, write, connect, apply, add, or make the change.

In implementation mode:

- Prefer `capabilities/*.json` for business workflow definitions.
- Prefer `atomics/*.json` for reusable actions.
- Put controlled business code under `executors/*`.
- Do not hardcode new business workflows into `app.py`.
- Modify `app.py` only when a missing generic framework feature blocks the design.

## Framework Rules

- The planner/AI may infer intent and parameters, but must not directly execute SQL, shell, HTTP, or credentialed operations.
- Treat AI-generated plans as untrusted input.
- Business workflows should invoke atomics with `executor.type=atomic` whenever the operation is reusable or sensitive.
- Do not bypass the atomic layer by calling a business script directly when the action should be audited, approved, or reused.
- Executor code must validate allowed operations, tables/resources, stages, parameters, limits, and risk.
- SQL must be parameterized. Reads should be allowlisted. Writes require explicit capability design and approval.
- Risky or write-capable atomics must set `requires_approval=true`.
- Atomic approval is bound to workflow id, stage, atomic name, and input hash. If input changes, it must be approved again.
- Reuse `dingtalk.notify` for notifications instead of implementing DingTalk sending inside business executors.
- Long-running tasks must be split into submit/check stages. Each invocation performs one action or one status check, then exits.
- Never generate long `while` loops or scripts that sleep for hours.
- If the completion rule is missing, stop at a plan or create a dry-run stub only.

## Capability Contract

Capabilities are JSON files under `capabilities/`.

Workflow stages should usually call atomics:

```json
{
  "name": "submit_pre_apasinfo",
  "label": "Submit pre_apasinfo",
  "executor": {
    "type": "atomic",
    "name": "history-data-repush.submit",
    "input": {
      "operation": "submit",
      "business_stage": "pre_apasinfo",
      "items": "$payload.items",
      "case_ids": "$payload.case_ids",
      "batch_size": "$payload.batch_size"
    }
  }
}
```

Use a separate check stage for asynchronous consumption:

```json
{
  "name": "check_pre_apasinfo",
  "label": "Check pre_apasinfo",
  "executor": {
    "type": "atomic",
    "name": "history-data-repush.check",
    "input": {
      "operation": "check",
      "business_stage": "pre_apasinfo",
      "case_ids": "$payload.case_ids"
    }
  },
  "poll_interval_seconds": 300,
  "max_wait_seconds": 21600
}
```

## Atomic Contract

Atomics are JSON files under `atomics/` and are loaded into `atomic_capabilities`. Every call is audited in `atomic_invocations`.

Supported atomic types:

- `notify`: queue a DingTalk notification through `NotificationRouter`.
- `command`: run a registered controlled executor.
- `mysql.read`: controlled read-only MySQL query.
- `http.request`: allowlisted HTTP request.

Command atomics call executors and inject `atomic_input` into stdin:

```json
{
  "workflow_id": "wf_xxx",
  "skill": "capability-name",
  "stage": "submit_pre_apasinfo",
  "payload": {},
  "executor": {},
  "stage_state": {},
  "atomic": {},
  "atomic_input": {
    "operation": "submit",
    "business_stage": "pre_apasinfo"
  }
}
```

Executors must print one JSON object:

```json
{
  "status": "running",
  "pending_count": 42,
  "success_count": 58,
  "failed_count": 0,
  "next_check_seconds": 300,
  "max_wait_seconds": 21600,
  "message": "Still processing"
}
```

Allowed statuses:

- `succeeded`: advance to next stage.
- `running`: keep current stage and poll later.
- `failed`: trigger failure/recovery handling.

## Implementation Checklist

1. Inspect `CLAUDE_PROJECT_CONTEXT.md`, `app.py`, `capabilities/`, `atomics/`, and related executors before editing.
2. Parse the user request into workflow stages, atomics, inputs, polling, notification, approval, and recovery rules.
3. Reuse existing atomics first.
4. Generate or update capability JSON, atomic JSON, and controlled executor code.
5. Compile changed Python files.
6. If only registries/executors changed, reload:

```powershell
Invoke-RestMethod -Method POST -Uri "http://127.0.0.1:8787/api/admin/reload-capabilities"
```

7. Restart only when `app.py` or runtime framework code changed.
8. Test through `/api/messages`; do not only call internal functions.
9. For framework-level changes, run:

```powershell
.\scripts\integration-entrypoint-test.ps1
```

## Response Shape

Plan-only response:

1. Parsed capability spec
2. Workflow stages
3. Atomics and controlled executors
4. Polling, timeout, notification, approval, and recovery policy
5. Assumptions and blocking questions
6. Exact phrase the user can say to proceed

Implementation response:

1. Parsed spec
2. Files changed
3. Tests run
4. How to test from DingTalk or `/api/messages`
