# Skill Integration

## Skill 的角色

Skill 负责把用户的自然语言需求整理成能力规格和 workflow 计划。它不直接持有密钥，不直接访问数据库，不直接执行长任务。

Skill 应该输出：

- 能力解决什么问题。
- 需要哪些输入。
- 需要哪些原子能力。
- workflow 阶段。
- 通知点。
- 轮询和超时策略。
- 审批和恢复策略。

## 接入原则

业务能力应通过 `atomic-capability-builder` 从用户描述生成。框架本身只保留通用能力：

- Orchestrator
- AI planner
- Workflow executor
- ToolGateway
- Notification router
- Approval flow
- Dashboard

新增业务能力时，先输出计划；只有用户明确要求实现时才修改代码。

## 当前接入形态

新能力不应该继续写进 `app.py` 的流程分支里，而是拆成两部分：

- `capabilities/*.json`：声明能力名称、意图、触发词、阶段、阶段执行器和默认入参。
- `executors/*` 或受控脚本/程序：真正访问数据库、日志、内部 API 的执行入口。

服务启动时会加载 `capabilities/`。服务运行中新增或调整能力后，调用：

```powershell
Invoke-RestMethod -Method POST -Uri "http://127.0.0.1:8787/api/admin/reload-capabilities"
```

这样新会话可以直接看到最新能力，不需要重启主服务。AI planner 会在提示词里看到已注册能力列表，再决定应该启动哪个 workflow。

## 长任务要求

等待异步消费、外部任务进度、日志出现、数据库状态变化等场景，必须使用框架的非阻塞轮询：

- submit/insert 阶段只提交一次任务或写一次数据。
- wait/check 阶段只检查一次状态。
- 未完成时返回 `status=running` 和 `next_check_seconds`。
- 框架记录 `next_check_at`，到点后再次唤起同一个 stage executor。
- executor 脚本不能自己 `while sleep` 等几小时。

这样长任务由 `workflow_stages` 和调度器管控，可以被 dashboard、状态查询、取消、超时和恢复逻辑统一复用。
