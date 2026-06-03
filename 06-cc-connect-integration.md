# cc-connect Integration

This MVP integrates with `cc-connect` through a new lightweight agent adapter.

Files added under `D:\cc-connect`:

```text
D:\cc-connect\agent\orchestrator\orchestrator.go
D:\cc-connect\cmd\cc-connect\plugin_agent_orchestrator.go
```

## Flow

```text
DingTalk
  -> cc-connect platform/dingtalk
  -> cc-connect agent/orchestrator
  -> POST http://127.0.0.1:8787/api/messages
  -> local Orchestrator / Workflow / Tool Gateway
```

The agent also polls:

```text
GET http://127.0.0.1:8787/api/notifications?conversation_id=<cc-connect-session-key>
```

Those notification rows are emitted as unsolicited cc-connect agent events, so stage progress can be sent back to DingTalk by cc-connect.

## Example Config

See:

```text
D:\dingtalk-claude-agent-architecture\cc-connect-orchestrator.example.toml
```

Copy the relevant blocks into your real `cc-connect` config and fill DingTalk credentials.

## Current Boundary

This integration makes `cc-connect` the IM gateway only.

Long-running tasks, state, tools, auditing, and notifications still live in:

```text
D:\dingtalk-claude-agent-architecture\app.py
```
