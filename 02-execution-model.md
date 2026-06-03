# Execution Model

## 核心判断

```text
规则明确、可验证、要可靠执行的，用代码。
输入模糊、需要理解、需要总结解释的，用大模型。
```

## 执行类型

### Interactive Plan

适合短交互：

- 查日志。
- 查订单状态。
- 查任务状态。
- 小范围数据查询。
- 调用工具后总结。

典型流程：

```text
用户问题 -> Claude 解析 -> Tool Gateway -> Claude 总结 -> 钉钉回复
```

### Workflow Plan

适合长任务和多阶段任务：

- 写入数据后等待消费。
- 阶段 A 完成后执行阶段 B。
- 需要重试、取消、恢复。
- 运行时间超过 1 分钟。

典型流程：

```text
用户确认 -> Claude 生成 plan -> Workflow Executor 接管 -> 后台执行和通知
```

### Watch Plan

适合监控：

- 每几分钟查询一次状态。
- 条件满足时通知。
- 长时间观察日志、指标或业务任务。

典型流程：

```text
Claude 注册 watch -> Watch Executor 周期执行 -> Notification Router 通知
```

## 什么时候用代码

- 插入数据。
- 查询数据库。
- 查询日志 API。
- 判断 `pending_count == 0`。
- 每 5 分钟轮询。
- 失败重试。
- 超时取消。
- 权限校验。
- 审批状态判断。
- 发进度通知。

## 什么时候用大模型

- 用户输入不规范。
- Excel 列名不明确。
- 用户只描述现象，需要判断查哪个服务。
- 日志结果很多，需要总结异常模式。
- 工具返回未知错误，需要诊断建议。
- 用户追问“为什么失败”。
- 需要生成面向人的报告。

## Workflow 节点类型

```json
{
  "type": "tool_call | wait_until | branch | approval | notify | llm_step | human_input"
}
```

推荐默认策略：

- `tool_call`：代码执行。
- `wait_until`：代码执行。
- `branch`：代码执行。
- `approval`：代码发钉钉卡片，等待用户动作。
- `notify`：代码模板化通知。
- `llm_step`：调用 Claude。
- `human_input`：等待钉钉用户输入。

## 风险边界

大模型可以建议高风险动作，但不能直接放行。

```text
Claude: 建议查询生产库。
Policy Engine: 判断权限、表白名单、行数限制、审批要求。
Workflow Executor: 审批通过后执行。
```
