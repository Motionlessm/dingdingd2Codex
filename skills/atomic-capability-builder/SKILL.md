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

Default to discovery/plan-only when the user describes a new capability for the first time, asks to plan, design, analyze, review, inspect, or says they only want a plan.

In discovery/plan-only mode:

- Do not edit files.
- Do not create executors.
- Do not restart services.
- Do not run mutating tests.
- Infer task type and available information from the user's natural-language description.
- Ask only for missing information that cannot be safely inferred.
- Prefer sensible defaults for low-risk details, but clearly label them as defaults.
- Return the parsed capability spec, workflow stages, atomics, polling policy, notification policy, approval policy, recovery policy, assumptions, and blocking questions.

Implementation mode applies only after the user has confirmed the plan or explicitly asks to implement, write, connect, apply, add, or make the change.

In implementation mode:

- Prefer `capabilities/*.json` for business workflow definitions.
- Prefer `atomics/*.json` for reusable actions.
- Put controlled business code under `executors/*`.
- Do not hardcode new business workflows into `app.py`.
- Modify `app.py` only when a missing generic framework feature blocks the design.

## Discovery Flow

The normal user should not need to describe workflow, atomic, executor, approval, and polling details. The skill must derive those from the project rules.

Use this flow for a new capability:

1. Identify the task type:
   - database write
   - database read
   - log lookup
   - HTTP/API call
   - long-running asynchronous workflow
   - notification-only workflow
   - mixed workflow
2. Extract known facts from the user description.
3. Infer safe defaults from the framework.
4. Ask at most 3-5 concise questions for critical gaps.
5. Produce a plan for user confirmation.
6. Implement only after the user confirms the plan.

Critical gaps by task type:

- Database write: target table, allowed operation, input fields, column mapping, allowed values, DSN environment variable, completion rule, approval requirement.
- Database read: data source, allowed tables, query filters, result limit, sensitive field handling.
- Log lookup: log source, service/app name, searchable fields, time range, result limit, redaction rule.
- HTTP/API call: allowed hosts, method, endpoint, input mapping, timeout, retry, approval requirement.
- Long-running workflow: submit/check split, polling interval, max wait, success/failure rule, cancellation behavior.
- Notification: when to notify, recipient conversation, message summary fields.

Ask only for information that is both missing and unsafe to guess. Examples:

- If a DB write description gives the table and fields but not the DSN variable, ask for the DSN environment variable name or propose a default.
- If the completion rule is missing, ask how to distinguish pending/success/failed or propose a conservative default.
- If the operation is write/destructive, do not ask whether approval is needed; default to `requires_approval=true` and mention it in the plan.

For a low-detail user request, respond like this:

```text
我识别到这是：长任务 workflow + 数据库写入 + 异步检查 + 钉钉通知。

我能推断：
- 三阶段顺序：...
- submit 是高风险写操作，需要 atomic 审批
- check 是低风险查询/检查
- 通知复用 dingtalk.notify

还缺 3 个关键信息：
1. 数据库连接环境变量名是什么？默认用 RETRY_PUSH_DB_DSN 可以吗？
2. result 字段如何判断 pending/success/failed？默认空或 pending=处理中，fail/error/失败=失败，其他非空=成功，可以吗？
3. 轮询间隔和最长等待？默认 5 分钟轮询，最长 6 小时，可以吗？

你回复“都按默认”或补充上述信息后，我会先给执行计划，不会直接改文件。
```

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

1. Task type and inferred facts
2. Missing critical information, max 3-5 questions
3. Proposed defaults
4. Parsed capability spec
5. Workflow stages
6. Atomics and controlled executors
7. Polling, timeout, notification, approval, and recovery policy
8. Exact phrase the user can say to confirm implementation

Implementation response:

1. Parsed spec
2. Files changed
3. Tests run
4. How to test from DingTalk or `/api/messages`
