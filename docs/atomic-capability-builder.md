# Atomic Capability Builder

`scripts/capability_builder.py` turns a natural-language task description into project file drafts:

- `capabilities/<name>.json`
- `atomics/<name>-submit.json`
- `atomics/<name>-check.json`
- `executors/<name>.py`

It is intentionally conservative. Generated executors are stubs until a developer fills in the controlled business operation.

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
