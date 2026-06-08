# Atomic Capability Builder

`scripts/capability_builder.py` turns a natural-language task description into project file drafts:

- `capabilities/<name>.json`
- `atomics/<name>-submit.json`
- `atomics/<name>-check.json`
- `executors/<name>.py`

It is intentionally conservative. Generated executors are stubs until a developer fills in the controlled business operation.

For reusable or sensitive actions, generated workflows should call `executor.type=atomic`.
The atomic can invoke a controlled executor when code is needed. This keeps
approval, audit, reload, and reuse in the framework instead of hiding those
concerns inside one business script.

## Discovery Before Implementation

New capability work should start from a short business description, not a long
framework prompt. The builder/skill should infer task type, known facts,
defaults, and missing information first.

Normal flow:

1. User describes the business goal in plain language.
2. Skill identifies task type, such as database write, log lookup, HTTP call,
   long-running workflow, or notification.
3. Skill asks only for critical gaps that cannot be safely inferred.
4. Skill produces a plan for confirmation.
5. Only after confirmation should files be written or registries reloaded.

For database write and long-running workflows, common critical gaps are DSN
environment variable, allowed table/columns, success/failure rule, polling
interval, and max wait. Risky writes default to atomic approval.

Example low-detail request:

```text
我要做历史数据重推，单号写 exception_to_atg_data，分 pre_apasinfo、pre_accept、pre_transact 三步，每步完成通知。
```

The expected first response is not file generation. It should identify the task
as database write + long-running workflow + notification, list inferred stages
and atomics, propose safe defaults, and ask only for critical missing data such
as the DSN environment variable.

## Dry Run

```powershell
python .\scripts\capability_builder.py --description "Create a workflow that reads MySQL order logs and sends DingTalk notification"
```

## From File

```powershell
python .\scripts\capability_builder.py --file .\requirement.txt
```

## Apply

```powershell
python .\scripts\capability_builder.py --file .\requirement.txt --apply
```

The script refuses to overwrite existing generated files.

## After Apply

```powershell
python -m py_compile .\app.py .\executors\*.py
Invoke-RestMethod -Method POST -Uri "http://127.0.0.1:8787/api/admin/reload-capabilities"
Invoke-RestMethod -Method GET -Uri "http://127.0.0.1:8787/api/capabilities"
```

For framework-level changes, run the entrypoint integration test:

```powershell
.\scripts\integration-entrypoint-test.ps1
```

## Generic Atomics

## Atomic-Level Approval

Atomic capabilities can require approval independently from workflow stages. Set the
atomic registry file like this:

```json
{
  "requires_approval": true,
  "risk": "high"
}
```

When a workflow invokes that atomic for the first time, the ToolGateway does not
run the executor immediately. It records the invocation as `waiting_approval`
and asks the WorkflowExecutor to create a DingTalk approval request.

The approval is bound to:

- workflow id
- workflow stage
- atomic capability name
- SHA-256 hash of the atomic input payload

After `/approve <approval_id>`, the workflow is resumed and the same stage is
retried. The ToolGateway only executes the atomic when the approved input hash
matches the current request. If the stage tries to call the same atomic with
different input, it must be approved again.

Use this for high-risk generic abilities such as database writes, destructive
HTTP calls, log export, account operations, or retry submission. Low-risk
read-only atomics can remain unapproved.

### `dingtalk.notify`

Queues and delivers a short notification to the source DingTalk conversation.

### `mysql.read`

Runs controlled read-only MySQL queries through `executors/atomic_mysql.py`.

Requirements:

- `ATOMIC_MYSQL_DSN=mysql://user:password@host:3306/database?charset=utf8mb4`
- SQL must start with `SELECT` or `WITH`.
- Write/DDL keywords are rejected.
- `allowed_tables` can be configured in `atomics/mysql-read.json`.

### `http.request`

Runs allowlisted HTTP GET/POST requests through `executors/atomic_http.py`.

Requirements:

- Configure `allowed_hosts` in `atomics/http-request.json`, or set `ATOMIC_HTTP_ALLOWED_HOSTS=host1,host2`.
- Only configured methods are allowed.
- Response size is capped by `max_response_bytes`.
