# Approval Flow

审批由 Orchestrator 状态机管理，不依赖本地聊天会话保持在线。

## 适用场景

- 写数据库。
- 清理后重提。
- 批量变更。
- 生产系统高风险操作。
- AI recovery planner 建议的中高风险动作。

## 命令

```text
approvals
approval appr_xxx
approve appr_xxx
reject appr_xxx
```

钉钉中优先使用无斜杠命令，避免 cc-connect 内置命令拦截。
