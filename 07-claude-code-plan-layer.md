# AI Plan Layer

AI plan layer 负责把自然语言转换成结构化计划。

当前推荐使用后端 API planner，而不是依赖 Claude Code CLI 非交互模式。

## 职责

- 识别 intent。
- 提取参数。
- 选择已注册原子能力。
- 输出 workflow plan 或 chat reply。

## 边界

- AI 不直接执行工具。
- AI 输出必须由 Orchestrator 校验。
- 未注册能力不能执行。
- 高风险动作必须走审批。

业务能力的 planner 可见描述由 `atomic-capability-builder` 生成和维护。
