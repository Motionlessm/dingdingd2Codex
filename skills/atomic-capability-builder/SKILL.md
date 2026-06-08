---
name: atomic-capability-builder
description: Generate and integrate DingTalk Codex Agent capabilities, atomics, and executors from natural-language task descriptions.
---

# Atomic Capability Builder

Use this skill when the user describes a new DingTalk workflow or internal ability and wants it turned into project files.

Project root:

```text
D:\dingdingd2Codex
```

Rules:

- Do not put secrets, DSNs, tokens, or private endpoints into generated files.
- Prefer reusable `atomics/*.json` for notification, database, HTTP, log, and approval actions.
- Put business workflows under `capabilities/*.json`.
- Put controlled code under `executors/*`.
- Long-running tasks must be submit/check style. Executor invocations must not sleep for hours.
- Write operations require approval unless explicitly designed as safe.

Fast path:

```powershell
python scripts/capability_builder.py --file requirement.txt
python scripts/capability_builder.py --file requirement.txt --apply
```

After applying:

```powershell
python -m py_compile app.py executors/*.py
Invoke-RestMethod -Method POST -Uri "http://127.0.0.1:8787/api/admin/reload-capabilities"
```
