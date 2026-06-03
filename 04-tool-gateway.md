# Tool Gateway

Tool Gateway 是执行层访问内部系统的唯一入口。

## 规则

- Workflow 不能直接访问数据库、日志平台、HTTP 内部接口或脚本。
- AI planner 只能看到脱敏后的能力描述和参数 schema。
- 密钥、DSN、token 只能放在 executor、环境变量或本地私有配置里。
- 写操作默认需要审批。
- 长任务必须拆成 submit/check 或 start/poll 阶段。

## 能力注册内容

每个原子能力至少包含：

```json
{
  "name": "capability_name",
  "description": "business purpose",
  "input_schema": {},
  "output_schema": {},
  "risk": "low|medium|high",
  "requires_approval": false,
  "executor": {
    "type": "command|http|queue",
    "target": "controlled execution entry"
  },
  "timeout_seconds": 30
}
```

业务能力由 `atomic-capability-builder` 根据用户描述生成规格和接入计划。

## 动态能力目录

当前运行框架使用 `capabilities/*.json` 注册 workflow 能力。示例：

```json
{
  "name": "logs-search",
  "label": "日志查询",
  "intent": "logs_search",
  "aliases": ["logs_search"],
  "triggers": ["查日志", "日志", "logs"],
  "created_message": "已创建日志查询任务",
  "input_defaults": {
    "service": "default-service",
    "keyword": "",
    "level": "error"
  },
  "stages": [
    {
      "name": "search_logs",
      "label": "检索日志",
      "executor": {
        "type": "command",
        "command": ["python", "executors/search_logs.py"],
        "timeout_seconds": 60
      }
    }
  ]
}
```

## 原子能力执行层

可复用原子能力放在 `atomics/*.json`，服务启动或热加载时同步到 SQLite 表 `atomic_capabilities`。每次调用都会写入 `atomic_invocations` 审计表。

当前支持的原子能力类型：

- `notify`：通过框架 `NotificationRouter` 发当前 workflow 通知。
- `command`：调用一个受控 command executor。

workflow stage 可以通过 `executor.type=atomic` 调用原子能力：

```json
{
  "name": "notice",
  "label": "发送通知",
  "executor": {
    "type": "atomic",
    "name": "dingtalk.notify",
    "input": {
      "message": "任务 {workflow_id} 阶段 {stage} 已完成"
    }
  }
}
```

查看和热加载：

```powershell
Invoke-RestMethod -Method GET -Uri "http://127.0.0.1:8787/api/atomics"
Invoke-RestMethod -Method POST -Uri "http://127.0.0.1:8787/api/admin/reload-atomics"
```

业务 executor 不应该重复实现已存在的通用能力，例如通知、通用数据库读写、日志查询、HTTP 调用等；应优先注册为原子能力再在 workflow 中引用。

`command` executor 从 stdin 接收 JSON，包含 `workflow_id`、`skill`、`stage`、`payload` 和 `executor`。stdout 必须返回 JSON 对象，例如：

```json
{
  "status": "succeeded",
  "job_id": "job_001",
  "submitted_count": 1,
  "pending_count": 0,
  "success_count": 1,
  "failed_count": 0,
  "message": "阶段已完成"
}
```

## 非阻塞轮询

长任务的等待阶段不能在 executor 脚本里 `while` + `sleep` 等几小时。executor 每次只执行一次原子动作或一次状态检查，然后退出。

如果外部系统还没处理完，返回：

```json
{
  "status": "running",
  "pending_count": 12,
  "success_count": 88,
  "failed_count": 0,
  "next_check_seconds": 300,
  "max_wait_seconds": 21600,
  "message": "仍在处理，5 分钟后再次检查"
}
```

框架会把 `next_check_at`、`timeout_at`、`attempt_count` 和 `result_json` 写入 `workflow_stages`，到点后再次唤起同一个 stage executor。每次唤起都会写入 `workflow_stage_runs`，用于审计和排查。

`timeout_seconds` 只表示单次 executor 进程最长运行时间，不表示业务等待总时长。业务等待总时长使用 `max_wait_seconds`。

新增或修改能力后，调用 `POST /api/admin/reload-capabilities` 热加载。`GET /api/capabilities` 可查看当前注册表。
