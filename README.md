# DingTalk Codex Agent

一个本地 DingTalk Agent 编排框架：把钉钉作为入口，把大模型规划、长任务工作流、审批、通知、原子能力和受控执行器串起来。

## 能力概览

- 钉钉消息通过 cc-connect 进入本地 Orchestrator。
- Planner 负责理解自然语言并选择已注册 workflow。
- WorkflowExecutor 负责长任务状态、阶段推进、轮询、失败恢复和取消。
- ToolGateway 负责统一调用原子能力和受控执行器。
- Atomic capabilities 用于复用通知、日志、数据库脚本、HTTP 调用等内部能力。
- 敏感信息不进入 skill、prompt 或能力配置，统一通过环境变量或本地私有配置读取。

## 项目结构

```text
app.py                         主服务：API、Planner、WorkflowExecutor、ToolGateway、Dashboard
capabilities/*.json            workflow 能力注册文件，支持热加载
atomics/*.json                 原子能力注册文件，支持热加载
executors/*                    受控执行器脚本或程序
cc-connect-orchestrator.example.toml  cc-connect 钉钉接入示例配置
CLAUDE_PROJECT_CONTEXT.md      给新会话/Claude/Codex 看的项目上下文
run.ps1                        启动本地服务
smoke-test.ps1                 入口级冒烟测试
```

运行态文件不会提交到仓库：

```text
data/
cc-connect.local.toml
*.db
*.log
__pycache__/
```

## 启动 Orchestrator

```powershell
cd D:\dingdingd2Codex
.\run.ps1
```

默认服务地址：

```text
http://127.0.0.1:8787
```

常用入口：

```text
GET  /health
GET  /dashboard
POST /api/messages
GET  /api/capabilities
POST /api/admin/reload-capabilities
GET  /api/atomics
POST /api/admin/reload-atomics
GET  /api/workflows
GET  /api/notifications
```

## cc-connect 接入钉钉

1. 复制 `cc-connect-orchestrator.example.toml` 到私有配置文件，例如：

```powershell
Copy-Item .\cc-connect-orchestrator.example.toml .\cc-connect.local.toml
```

2. 填入钉钉 `client_id` 和 `client_secret`。

3. 用 cc-connect 启动：

```powershell
D:\cc-connect\cc-connect-orchestrator.exe --config D:\dingdingd2Codex\cc-connect.local.toml
```

`cc-connect.local.toml` 包含密钥，不要提交。

## 能力热加载

新增或修改 `capabilities/*.json`、`atomics/*.json` 后，不需要重启 Orchestrator：

```powershell
Invoke-RestMethod -Method POST -Uri "http://127.0.0.1:8787/api/admin/reload-capabilities"
Invoke-RestMethod -Method POST -Uri "http://127.0.0.1:8787/api/admin/reload-atomics"
```

## 冒烟测试

```powershell
cd D:\dingdingd2Codex
.\smoke-test.ps1
```

或手动从入口发送消息：

```powershell
Invoke-RestMethod -Method POST -Uri "http://127.0.0.1:8787/api/messages" `
  -ContentType "application/json; charset=utf-8" `
  -Body '{"conversation_id":"local-test","user_id":"u1","text":"查日志 payment error"}'
```

## 重要约束

- 新业务能力优先放到 `capabilities/*.json` 和 `executors/*`。
- 可复用动作优先注册成 `atomics/*.json`，不要在业务 executor 里重复实现通知、数据库访问、日志查询等通用能力。
- 长任务 executor 不要 `while + sleep` 等几小时；每次只执行一次提交或检查，返回 `running`、`next_check_seconds`、`max_wait_seconds` 交给 WorkflowExecutor 调度。
- 数据库连接、Token、DSN 等敏感信息只从环境变量或本地私有配置读取。
