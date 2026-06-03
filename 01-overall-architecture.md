# Overall Architecture

## 分层

```text
DingTalk Gateway
  -> Agent Orchestrator
    -> Claude / Skill Runtime
    -> Plan Builder
    -> Interactive Executor
    -> Workflow Executor
    -> Watch Executor
      -> Tool Gateway
        -> MySQL / Logs / Internal APIs
    -> State Store / Artifact Store
    -> Notification Router
      -> DingTalk
```

## 服务职责

### DingTalk Gateway

负责钉钉协议层能力：

- 接收钉钉机器人消息。
- 验签。
- 识别单聊、群聊、用户、消息 ID。
- 发送普通消息、Markdown、交互卡片。
- 更新长任务进度卡片。

它不负责业务逻辑，不直接调用数据库。

### Agent Orchestrator

平台核心控制层：

- 会话管理。
- 用户权限识别。
- Skill 路由。
- 调用 Claude/Skill Runtime。
- 接收结构化执行计划。
- 判断使用 Interactive、Workflow 还是 Watch 执行器。
- 创建任务、保存状态、处理取消和查询。

### Claude / Skill Runtime

负责智能部分：

- 理解用户输入。
- 解析不规范参数。
- 选择 Skill。
- 生成结构化 plan。
- 总结工具返回结果。
- 解释异常和给出建议。

不负责：

- 长时间等待。
- 创建后台子代理。
- 直接连接数据库。
- 直接发送钉钉 webhook。
- 保存密钥。

### Plan Builder

把 Claude/Skill 输出转成平台可执行计划。

计划类型：

- `interactive_plan`：短任务，适合查日志、查状态、轻量数据查询。
- `workflow_plan`：多步骤长任务，适合阶段推进、等待校验、恢复。
- `watch_plan`：监控任务，适合未来一段时间定期查询并按条件通知。

### Workflow Executor

负责长任务可靠执行：

- 调用工具。
- 定时器。
- 条件判断。
- 阶段推进。
- 重试。
- 超时。
- 取消。
- 恢复。

可以用 Temporal，也可以先用 Postgres + Redis/BullMQ 实现。

### Watch Executor

负责后台监控：

- 周期性调用状态工具。
- 只有状态变化、失败、完成、超时时通知。
- 不唤起本地 Claude 会话。

### Tool Gateway

所有内部能力统一入口：

- MySQL 查询。
- 日志查询。
- 业务表写入。
- 内部 HTTP API。
- 文件和报表生成。

统一处理：

- 权限。
- 参数校验。
- Secret 注入。
- 审计。
- 限流。
- 脱敏。
- 超时。

### State Store

保存任务状态：

- conversation。
- task。
- workflow。
- workflow stage。
- watch。
- approval。
- tool call audit。

推荐 Postgres。

### Artifact Store

保存大材料：

- 单号列表。
- Excel 原文件。
- 中间结果。
- 错误明细。
- 最终报告。

推荐 MinIO/S3，也可以先用本地文件 + Postgres 元数据。

### Notification Router

统一通知出口：

- 根据 `conversation_id` 找到钉钉会话。
- 控制进度通知频率。
- 长结果转成文件或报告链接。
- 防止后台任务把通知发到本地 Claude 会话。

## 关键原则

```text
Claude 会话是短生命周期推理。
Workflow 是长生命周期状态机。
Artifact 是任务材料的长期存储。
Tool Gateway 是内部系统访问边界。
```
