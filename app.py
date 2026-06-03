import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib import request as urlrequest


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "agent.db"
ARTIFACT_DIR = DATA_DIR / "artifacts"
LOG_DIR = DATA_DIR / "logs"
CAPABILITY_DIR = BASE_DIR / "capabilities"
ATOMIC_DIR = BASE_DIR / "atomics"
CC_CONNECT_EXE = Path(os.environ.get("CC_CONNECT_EXE", r"D:\cc-connect\cc-connect-orchestrator.exe"))
CC_CONNECT_DATA_DIR = Path(os.environ.get("CC_CONNECT_DATA_DIR", str(DATA_DIR / "cc-connect")))

CAPABILITY_REGISTRY = {}
CAPABILITY_LOCK = threading.RLock()


def normalize_skill(skill):
    return (skill or "").replace("_", "-")


def normalize_intent(intent):
    return (intent or "").replace("-", "_")


def load_capability_files():
    capabilities = {}
    CAPABILITY_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(CAPABILITY_DIR.glob("*.json")):
        with path.open("r", encoding="utf-8") as f:
            item = json.load(f)
        name = normalize_skill(item.get("name") or path.stem)
        stages = item.get("stages") or []
        if not name or not stages:
            raise ValueError(f"invalid capability file: {path}")
        normalized_stages = []
        for stage in stages:
            if isinstance(stage, str):
                normalized_stages.append({"name": stage, "label": stage, "executor": {"type": "builtin", "name": stage}})
            elif stage.get("name"):
                normalized_stages.append(stage)
            else:
                raise ValueError(f"invalid stage in capability file: {path}")
        item["name"] = name
        item["stages"] = normalized_stages
        item["_path"] = str(path)
        keys = {name, normalize_skill(item.get("intent") or name)}
        for alias in item.get("aliases") or []:
            keys.add(normalize_skill(alias))
        for key in keys:
            if key:
                capabilities[key] = item
    return capabilities


def reload_capabilities():
    loaded = load_capability_files()
    with CAPABILITY_LOCK:
        CAPABILITY_REGISTRY.clear()
        CAPABILITY_REGISTRY.update(loaded)
    return loaded


def all_capabilities():
    with CAPABILITY_LOCK:
        seen = set()
        items = []
        for cap in CAPABILITY_REGISTRY.values():
            name = cap["name"]
            if name not in seen:
                seen.add(name)
                items.append(cap)
        return items


def workflow_template(skill):
    normalized = normalize_skill(skill)
    with CAPABILITY_LOCK:
        return CAPABILITY_REGISTRY.get(normalized) or CAPABILITY_REGISTRY.get(skill)


def workflow_stages_for(skill):
    template = workflow_template(skill)
    if template is None:
        return []
    return [stage["name"] for stage in template["stages"]]


def workflow_stage_config(skill, stage_name):
    template = workflow_template(skill)
    if template is None:
        return None
    for stage in template["stages"]:
        if stage["name"] == stage_name:
            return stage
    return None


def capability_for_intent(intent):
    wanted = {normalize_skill(intent), normalize_skill(normalize_intent(intent))}
    with CAPABILITY_LOCK:
        for cap in CAPABILITY_REGISTRY.values():
            names = {
                normalize_skill(cap.get("name")),
                normalize_skill(cap.get("intent")),
                normalize_skill(normalize_intent(cap.get("intent"))),
            }
            for alias in cap.get("aliases") or []:
                names.add(normalize_skill(alias))
                names.add(normalize_skill(normalize_intent(alias)))
            if wanted & names:
                return cap
    return None


def capability_prompt_text():
    lines = []
    for cap in all_capabilities():
        stages = ", ".join(stage["name"] for stage in cap.get("stages") or [])
        triggers = ", ".join(cap.get("triggers") or [])
        lines.append(
            f"- intent={cap.get('intent') or cap['name']}; skill={cap['name']}; label={cap.get('label', cap['name'])}; "
            f"stages=[{stages}]; triggers=[{triggers}]"
        )
    return "\n".join(lines) or "- no registered workflow capabilities"


def load_atomic_files():
    atomics = []
    ATOMIC_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(ATOMIC_DIR.glob("*.json")):
        with path.open("r", encoding="utf-8") as f:
            item = json.load(f)
        name = item.get("name") or path.stem
        atomic_type = item.get("type")
        if not name or not atomic_type:
            raise ValueError(f"invalid atomic file: {path}")
        item["name"] = name
        item["_path"] = str(path)
        atomics.append(item)
    return atomics


def build_planner_prompt(text, conversation_id, user_id):
    return f"""
你是钉钉 Agent Orchestrator 的意图规划器。只做意图识别，不执行工具，不连接数据库，不启动后台任务。

请只返回一个 JSON 对象，不要 Markdown，不要解释。JSON 字段如下：
{{
  "plan_type": "workflow_plan | interactive_plan | chat | unsupported",
  "intent": "registered capability intent | status | cancel | chat | unsupported",
  "confidence": 0.0,
  "requires_confirmation": false,
  "reply": "",
  "case_ids": [],
  "stages": [],
  "batch_size": 2000,
  "service": "",
  "keyword": "",
  "level": "error",
  "minutes": 30,
  "workflow_id": ""
}}

已注册能力：
{capability_prompt_text()}

意图规则：
1. 命中已注册能力时，intent 必须返回能力列表里的 intent，并提取该能力需要的入参。
2. 查询任务状态 => intent=status，提取 workflow_id。
3. 取消任务 => intent=cancel，提取 workflow_id。
4. 其他普通问答 => intent=chat。

conversation_id={conversation_id}
user_id={user_id}
用户消息：{text}
""".strip()


def build_recovery_prompt(context):
    return f"""
你是后台 Workflow 的故障恢复规划器。只分析原因并输出受控恢复计划，不执行任何工具。

只能返回一个 JSON 对象，不要 Markdown，不要解释。JSON 字段如下：
{{
  "diagnosis": "失败原因简述",
  "action": "wait_and_retry | clear_stage_and_resubmit | mark_failed",
  "stage": "",
  "delay_seconds": 0,
  "requires_approval": false,
  "risk": "low | medium | high",
  "reason": "给审批人看的原因"
}}

动作规则：
1. wait_and_retry：适合下游消费延迟、临时超时、可等待后重试的情况。
2. clear_stage_and_resubmit：适合当前阶段数据需要清理并重新提交，必须 requires_approval=true，risk 至少 medium。
3. mark_failed：适合参数错误、不支持的 skill、无法自动恢复的情况。

只能选择上面三个 action，不允许输出其他动作。

失败上下文：
{json.dumps(context, ensure_ascii=False, indent=2)}
""".strip()


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def json_response(handler, status, body):
    data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def html_response(handler, status, body):
    data = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def append_jsonl(path, item):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def read_json(handler):
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8"))


class Store:
    def __init__(self, db_path):
        self.db_path = db_path
        self.lock = threading.RLock()

    def connect(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with self.lock, self.connect() as conn:
            conn.executescript(
                """
                create table if not exists workflows (
                    id text primary key,
                    skill text not null,
                    status text not null,
                    conversation_id text not null,
                    user_id text not null,
                    artifact_id text not null,
                    case_count integer not null,
                    current_stage_index integer not null default 0,
                    cancel_requested integer not null default 0,
                    created_at text not null,
                    updated_at text not null,
                    error text
                );

                create table if not exists workflow_stages (
                    workflow_id text not null,
                    stage text not null,
                    status text not null,
                    job_id text,
                    submitted_count integer not null default 0,
                    pending_count integer not null default 0,
                    success_count integer not null default 0,
                    failed_count integer not null default 0,
                    last_checked_at text,
                    created_at text not null,
                    updated_at text not null,
                    primary key (workflow_id, stage)
                );

                create table if not exists workflow_stage_runs (
                    id text primary key,
                    workflow_id text not null,
                    stage text not null,
                    attempt_no integer not null,
                    status text not null,
                    input_json text not null,
                    output_json text,
                    error text,
                    started_at text not null,
                    finished_at text
                );

                create table if not exists notifications (
                    id text primary key,
                    workflow_id text,
                    conversation_id text not null,
                    message text not null,
                    status text not null default 'pending',
                    delivery_attempts integer not null default 0,
                    last_error text,
                    delivered_at text,
                    created_at text not null,
                    updated_at text not null
                );

                create table if not exists pending_confirmations (
                    token text primary key,
                    conversation_id text not null,
                    user_id text not null,
                    artifact_id text not null,
                    case_count integer not null,
                    created_at text not null
                );

                create table if not exists approvals (
                    id text primary key,
                    workflow_id text not null,
                    conversation_id text not null,
                    requester_user_id text not null,
                    approver_user_id text,
                    status text not null,
                    action text not null,
                    risk_level text not null,
                    reason text not null,
                    payload_json text not null,
                    expires_at text not null,
                    created_at text not null,
                    updated_at text not null
                );

                create table if not exists atomic_capabilities (
                    name text primary key,
                    label text not null,
                    type text not null,
                    risk text not null,
                    requires_approval integer not null default 0,
                    enabled integer not null default 1,
                    config_json text not null,
                    source text not null,
                    updated_at text not null
                );

                create table if not exists atomic_invocations (
                    id text primary key,
                    atomic_name text not null,
                    workflow_id text,
                    stage text,
                    status text not null,
                    input_json text not null,
                    output_json text,
                    error text,
                    created_at text not null,
                    finished_at text
                );

                """
            )
            self._ensure_column(conn, "workflow_stages", "executor_json", "text")
            self._ensure_column(conn, "workflow_stages", "next_check_at", "text")
            self._ensure_column(conn, "workflow_stages", "attempt_count", "integer not null default 0")
            self._ensure_column(conn, "workflow_stages", "max_attempts", "integer not null default 0")
            self._ensure_column(conn, "workflow_stages", "timeout_at", "text")
            self._ensure_column(conn, "workflow_stages", "result_json", "text")
            self._ensure_column(conn, "atomic_capabilities", "description", "text")
            self._ensure_column(conn, "notifications", "status", "text not null default 'pending'")
            self._ensure_column(conn, "notifications", "delivery_attempts", "integer not null default 0")
            self._ensure_column(conn, "notifications", "last_error", "text")
            self._ensure_column(conn, "notifications", "delivered_at", "text")
            self._ensure_column(conn, "notifications", "updated_at", "text")
            conn.execute("update notifications set updated_at = created_at where updated_at is null")

    def _ensure_column(self, conn, table, column, ddl):
        columns = {row[1] for row in conn.execute(f"pragma table_info({table})")}
        if column not in columns:
            conn.execute(f"alter table {table} add column {column} {ddl}")

    def execute(self, sql, params=()):
        with self.lock, self.connect() as conn:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur

    def query_one(self, sql, params=()):
        with self.lock, self.connect() as conn:
            return conn.execute(sql, params).fetchone()

    def query_all(self, sql, params=()):
        with self.lock, self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def reload_atomics(self):
        items = load_atomic_files()
        now = now_iso()
        with self.lock, self.connect() as conn:
            conn.execute("update atomic_capabilities set enabled = 0, updated_at = ?", (now,))
            for item in items:
                name = item["name"]
                config = dict(item)
                config.pop("_path", None)
                conn.execute(
                    """
                    insert into atomic_capabilities(
                        name, label, type, risk, requires_approval, enabled,
                        config_json, source, updated_at, description
                    ) values (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                    on conflict(name) do update set
                        label = excluded.label,
                        type = excluded.type,
                        risk = excluded.risk,
                        requires_approval = excluded.requires_approval,
                        enabled = 1,
                        config_json = excluded.config_json,
                        source = excluded.source,
                        updated_at = excluded.updated_at,
                        description = excluded.description
                    """,
                    (
                        name,
                        item.get("label") or name,
                        item["type"],
                        item.get("risk") or "low",
                        1 if item.get("requires_approval") else 0,
                        json.dumps(config, ensure_ascii=False),
                        item.get("_path") or "",
                        now,
                        item.get("description") or "",
                    ),
                )
            conn.commit()
        return items


class ArtifactStore:
    def save_payload(self, payload):
        artifact_id = new_id("artifact")
        path = ARTIFACT_DIR / f"{artifact_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return artifact_id

    def load_payload(self, artifact_id):
        path = ARTIFACT_DIR / f"{artifact_id}.json"
        return json.loads(path.read_text(encoding="utf-8"))

class NotificationRouter:
    def __init__(self, store):
        self.store = store

    def send(self, conversation_id, message, workflow_id=None):
        notification_id = new_id("ntf")
        now = now_iso()
        self.store.execute(
            """
            insert into notifications(
                id, workflow_id, conversation_id, message, status,
                delivery_attempts, created_at, updated_at
            )
            values (?, ?, ?, ?, 'pending', 0, ?, ?)
            """,
            (notification_id, workflow_id, conversation_id, message, now, now),
        )
        print(f"[notify][pending][{conversation_id}] {message}", flush=True)
        self.deliver_async(notification_id, conversation_id, message)
        return notification_id

    def deliver_async(self, notification_id, conversation_id, message):
        if not CC_CONNECT_EXE.exists():
            return
        thread = threading.Thread(
            target=self._deliver_via_cc_connect,
            args=(notification_id, conversation_id, message),
            daemon=True,
        )
        thread.start()

    def _deliver_via_cc_connect(self, notification_id, conversation_id, message):
        now = now_iso()
        try:
            result = subprocess.run(
                [
                    str(CC_CONNECT_EXE),
                    "send",
                    "--data-dir",
                    str(CC_CONNECT_DATA_DIR),
                    "--session",
                    conversation_id,
                    "--message",
                    message,
                ],
                cwd=str(CC_CONNECT_EXE.parent),
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception as exc:
            self._record_delivery_failure(notification_id, str(exc))
            return
        if result.returncode == 0:
            self.store.execute(
                """
                update notifications
                set status = 'sent',
                    delivery_attempts = delivery_attempts + 1,
                    last_error = null,
                    delivered_at = ?,
                    updated_at = ?
                where id = ?
                """,
                (now, now, notification_id),
            )
            return
        error = (result.stderr or result.stdout or f"cc-connect send failed with exit code {result.returncode}").strip()
        self._record_delivery_failure(notification_id, error)

    def _record_delivery_failure(self, notification_id, error):
        now = now_iso()
        self.store.execute(
            """
            update notifications
            set delivery_attempts = delivery_attempts + 1,
                last_error = ?,
                updated_at = ?
            where id = ?
            """,
            (error[:1000], now, notification_id),
        )


class ToolGateway:
    def __init__(self, store, artifact_store, notifier=None):
        self.store = store
        self.artifacts = artifact_store
        self.notifier = notifier

    def execute_stage(self, workflow, stage_name, stage_config, stage_row=None):
        executor = stage_config.get("executor") or {}
        executor_type = executor.get("type", "builtin")
        if executor_type == "builtin":
            return self.execute_builtin_stage(workflow, stage_name, executor)
        if executor_type == "command":
            return self.execute_command_stage(workflow, stage_name, executor, stage_row)
        if executor_type == "atomic":
            return self.execute_atomic_stage(workflow, stage_name, executor, stage_row)
        raise ValueError(f"unsupported executor type: {executor_type}")

    def execute_builtin_stage(self, workflow, stage_name, executor):
        name = executor.get("name") or stage_name
        payload = self.artifacts.load_payload(workflow["artifact_id"])
        workflow_id = workflow["id"]
        if name == "logs_parse_query":
            message = (
                f"日志查询任务 {workflow_id} 已解析参数："
                f"服务={payload.get('service', 'default-service')}，"
                f"关键字={payload.get('keyword', '')}，"
                f"级别={payload.get('level', 'error')}，"
                f"范围={payload.get('minutes', 30)} 分钟。"
            )
            return {"status": "succeeded", "message": message}
        if name == "logs_search":
            result = self.logs_search(
                service=payload.get("service") or "default-service",
                keyword=payload.get("keyword") or "",
                level=payload.get("level") or "error",
                minutes=int(payload.get("minutes") or 30),
                limit=int(payload.get("limit") or 20),
            )
            item = result["items"][0] if result["items"] else {"timestamp": "", "message": "无日志"}
            message = f"日志查询任务 {workflow_id} 已完成检索，样例：{item['timestamp']} {item['message']}"
            return {"status": "succeeded", "message": message, "result": result}
        if name == "logs_summarize":
            return {
                "status": "succeeded",
                "message": f"日志查询任务 {workflow_id} 已生成摘要，建议在通知记录或状态中查看结果。",
            }
        raise ValueError(f"unsupported builtin executor: {name}")

    def execute_command_stage(self, workflow, stage_name, executor, stage_row=None):
        return self.execute_command(workflow, stage_name, executor, stage_row)

    def execute_command(self, workflow, stage_name, executor, stage_row=None, extra_input=None):
        command = executor.get("command")
        path = executor.get("path")
        if isinstance(command, list) and command:
            cmd = [str(part) for part in command]
        elif command:
            cmd = [str(command)]
        elif path:
            command_path = Path(path)
            if not command_path.is_absolute():
                command_path = BASE_DIR / command_path
            cmd = [str(command_path)]
        else:
            raise ValueError("command executor requires path or command")
        for arg in executor.get("args") or []:
            cmd.append(str(arg))
        payload = self.artifacts.load_payload(workflow["artifact_id"])
        input_payload = {
            "workflow_id": workflow["id"],
            "skill": workflow["skill"],
            "stage": stage_name,
            "payload": payload,
            "executor": executor,
            "stage_state": dict(stage_row) if stage_row is not None else None,
        }
        if extra_input:
            input_payload.update(extra_input)
        timeout_seconds = int(executor.get("timeout_seconds") or 60)
        completed = subprocess.run(
            cmd,
            input=json.dumps(input_payload, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            cwd=str(BASE_DIR),
            timeout=timeout_seconds,
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or "").strip())
        output = (completed.stdout or "").strip()
        if not output:
            return {"status": "succeeded"}
        result = json.loads(output)
        if not isinstance(result, dict):
            raise ValueError("command executor must return a JSON object")
        return result

    def execute_atomic_stage(self, workflow, stage_name, executor, stage_row=None):
        atomic_name = executor.get("name") or executor.get("atomic") or executor.get("capability")
        if not atomic_name:
            raise ValueError("atomic executor requires name")
        input_data = self.render_atomic_input(executor.get("input") or {}, workflow, stage_name, stage_row)
        return self.invoke_atomic(atomic_name, workflow, stage_name, input_data, stage_row)

    def render_atomic_input(self, value, workflow, stage_name, stage_row):
        payload = self.artifacts.load_payload(workflow["artifact_id"])
        context = {
            "workflow_id": workflow["id"],
            "skill": workflow["skill"],
            "stage": stage_name,
            "conversation_id": workflow["conversation_id"],
            "user_id": workflow["user_id"],
            "payload": payload,
            "stage_state": dict(stage_row) if stage_row is not None else {},
        }

        def resolve(expr):
            parts = expr.split(".")
            current = context
            for part in parts:
                if isinstance(current, dict) and part in current:
                    current = current[part]
                else:
                    return ""
            return current

        def render(item):
            if isinstance(item, str):
                if item.startswith("$"):
                    return resolve(item[1:])
                def repl(match):
                    return str(resolve(match.group(1)))
                return re.sub(r"\{([A-Za-z0-9_.]+)\}", repl, item)
            if isinstance(item, list):
                return [render(x) for x in item]
            if isinstance(item, dict):
                return {k: render(v) for k, v in item.items()}
            return item

        return render(value)

    def invoke_atomic(self, atomic_name, workflow, stage_name, input_data, stage_row=None):
        row = self.store.query_one(
            "select * from atomic_capabilities where name = ? and enabled = 1",
            (atomic_name,),
        )
        if row is None:
            raise ValueError(f"atomic capability not found or disabled: {atomic_name}")
        config = json.loads(row["config_json"] or "{}")
        invocation_id = new_id("atomic")
        created = now_iso()
        self.store.execute(
            """
            insert into atomic_invocations(
                id, atomic_name, workflow_id, stage, status, input_json, created_at
            ) values (?, ?, ?, ?, 'running', ?, ?)
            """,
            (
                invocation_id,
                atomic_name,
                workflow["id"],
                stage_name,
                json.dumps(input_data, ensure_ascii=False),
                created,
            ),
        )
        try:
            if row["requires_approval"]:
                raise RuntimeError(f"atomic capability requires approval: {atomic_name}")
            atomic_type = row["type"]
            if atomic_type == "notify":
                result = self.invoke_notify_atomic(config, workflow, input_data)
            elif atomic_type == "command":
                command_executor = dict(config.get("executor") or {})
                if not command_executor:
                    command_executor = {
                        "command": config.get("command"),
                        "path": config.get("path"),
                        "args": config.get("args") or [],
                        "timeout_seconds": config.get("timeout_seconds") or 60,
                    }
                result = self.execute_command(
                    workflow,
                    stage_name,
                    command_executor,
                    stage_row,
                    extra_input={"atomic": config, "atomic_input": input_data},
                )
            else:
                raise ValueError(f"unsupported atomic type: {atomic_type}")
            finished = now_iso()
            self.store.execute(
                """
                update atomic_invocations
                set status = ?, output_json = ?, finished_at = ?
                where id = ?
                """,
                (
                    result.get("status") or "succeeded",
                    json.dumps(result, ensure_ascii=False),
                    finished,
                    invocation_id,
                ),
            )
            result.setdefault("atomic_invocation_id", invocation_id)
            return result
        except Exception as exc:
            finished = now_iso()
            self.store.execute(
                """
                update atomic_invocations
                set status = 'failed', error = ?, finished_at = ?
                where id = ?
                """,
                (str(exc), finished, invocation_id),
            )
            raise

    def invoke_notify_atomic(self, config, workflow, input_data):
        message = input_data.get("message") or config.get("message")
        if not message and config.get("template"):
            message = config["template"].format(**{k: str(v) for k, v in input_data.items()})
        if not message:
            raise ValueError("notify atomic requires message")
        conversation_id = input_data.get("conversation_id") or workflow["conversation_id"]
        if self.notifier is None:
            return {"status": "succeeded", "message": message}
        self.notifier.send(conversation_id, message, workflow["id"])
        return {
            "status": "succeeded",
            "submitted_count": 1,
            "pending_count": 0,
            "success_count": 1,
            "failed_count": 0,
            "notification_sent": True,
            "summary": message,
        }

    def logs_search(self, service, keyword="", level="error", minutes=30, limit=20):
        # Placeholder for a real log backend. This keeps the same Gateway shape
        # as production without requiring credentials.
        return {
            "service": service,
            "keyword": keyword,
            "level": level,
            "minutes": minutes,
            "items": [
                {
                    "timestamp": now_iso(),
                    "level": level,
                    "message": f"mock log from {service}; keyword={keyword or '-'}",
                }
            ][:limit],
        }

    def simulate_consume(self, batch_size):
        return 0


class ClaudeCodeClient:
    def __init__(self, enabled=True, command="claude", timeout_seconds=15, model=""):
        self.enabled = enabled
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.model = model

    def available(self):
        return self.enabled and self._resolve_command() is not None

    def _resolve_command(self):
        if os.name == "nt" and self.command == "claude":
            return shutil.which("claude.cmd") or shutil.which("claude.exe") or shutil.which("claude")
        return shutil.which(self.command)

    def create_plan(self, text, conversation_id, user_id):
        if not self.available():
            raise RuntimeError("Claude Code CLI is not available")

        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "plan_type": {
                    "type": "string",
                    "enum": ["workflow_plan", "interactive_plan", "chat", "unsupported"],
                },
                "intent": {
                    "type": "string",
                    "enum": ["logs_search", "status", "cancel", "chat", "unsupported"],
                },
                "confidence": {"type": "number"},
                "requires_confirmation": {"type": "boolean"},
                "reply": {"type": "string"},
                "case_ids": {"type": "array", "items": {"type": "string"}},
                "stages": {"type": "array", "items": {"type": "string"}},
                "batch_size": {"type": "integer"},
                "service": {"type": "string"},
                "keyword": {"type": "string"},
                "level": {"type": "string"},
                "minutes": {"type": "integer"},
                "workflow_id": {"type": "string"},
            },
            "required": ["plan_type", "intent", "confidence", "requires_confirmation", "reply"],
        }

        prompt = build_planner_prompt(text, conversation_id, user_id)

        executable = self._resolve_command()
        if executable is None:
            raise RuntimeError("Claude Code CLI is not available")

        cmd = [
            executable,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--no-session-persistence",
            "--max-budget-usd",
            "0.10",
        ]
        if self.model:
            cmd.extend(["--model", self.model])
        completed = run_command_with_tree_timeout(cmd, cwd=str(BASE_DIR), timeout_seconds=self.timeout_seconds)
        if completed["returncode"] != 0:
            raise RuntimeError((completed["stderr"] or completed["stdout"]).strip())

        outer = json.loads(completed["stdout"])
        result = outer.get("result", outer)
        if isinstance(result, dict):
            plan = result
        else:
            plan = self._parse_json_text(str(result))
        return self._normalize_plan(plan)

    def _parse_json_text(self, text):
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
        return json.loads(text)

    def _normalize_plan(self, plan):
        plan.setdefault("case_ids", [])
        plan.setdefault("stages", [])
        plan.setdefault("batch_size", 2000)
        plan.setdefault("service", "")
        plan.setdefault("keyword", "")
        plan.setdefault("level", "error")
        plan.setdefault("minutes", 30)
        plan.setdefault("workflow_id", "")
        return plan


class OpenAICompatiblePlannerClient:
    def __init__(self, enabled=True, base_url="", api_key="", model="", timeout_seconds=20):
        self.enabled = enabled
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.timeout_seconds = timeout_seconds

    def available(self):
        return bool(self.enabled and self.base_url and self.api_key and self.model)

    def create_plan(self, text, conversation_id, user_id):
        if not self.available():
            raise RuntimeError("API planner is not configured")

        prompt = build_planner_prompt(text, conversation_id, user_id)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你只返回 JSON 对象，不返回 Markdown。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urlrequest.Request(
            f"{self.base_url}/chat/completions",
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        with urlrequest.urlopen(req, timeout=self.timeout_seconds) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        result = json.loads(body)
        content = result["choices"][0]["message"]["content"]
        return self._normalize_plan(self._parse_json_text(content))

    def create_recovery_plan(self, context):
        if not self.available():
            raise RuntimeError("API planner is not configured")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你只返回 JSON 对象，不返回 Markdown。"},
                {"role": "user", "content": build_recovery_prompt(context)},
            ],
            "temperature": 0,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urlrequest.Request(
            f"{self.base_url}/chat/completions",
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        with urlrequest.urlopen(req, timeout=self.timeout_seconds) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        result = json.loads(body)
        content = result["choices"][0]["message"]["content"]
        return self._normalize_recovery_plan(self._parse_json_text(content))

    def _parse_json_text(self, text):
        text = (text or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
        return json.loads(text)

    def _normalize_plan(self, plan):
        plan.setdefault("plan_type", "chat")
        plan.setdefault("intent", "chat")
        plan.setdefault("confidence", 0.0)
        plan.setdefault("requires_confirmation", False)
        plan.setdefault("reply", "")
        plan.setdefault("case_ids", [])
        plan.setdefault("stages", [])
        plan.setdefault("batch_size", 2000)
        plan.setdefault("service", "")
        plan.setdefault("keyword", "")
        plan.setdefault("level", "error")
        plan.setdefault("minutes", 30)
        plan.setdefault("workflow_id", "")
        return plan

    def _normalize_recovery_plan(self, plan):
        allowed = {"wait_and_retry", "clear_stage_and_resubmit", "mark_failed"}
        plan.setdefault("diagnosis", "")
        plan.setdefault("action", "mark_failed")
        plan.setdefault("stage", "")
        plan.setdefault("delay_seconds", 0)
        plan.setdefault("requires_approval", False)
        plan.setdefault("risk", "medium")
        plan.setdefault("reason", plan.get("diagnosis") or "AI recovery planner requested action.")
        if plan["action"] not in allowed:
            plan["action"] = "mark_failed"
        if plan["action"] == "clear_stage_and_resubmit":
            plan["requires_approval"] = True
            if plan["risk"] == "low":
                plan["risk"] = "medium"
        return plan


class AnthropicCompatiblePlannerClient:
    def __init__(self, enabled=True, base_url="", auth_token="", model="", timeout_seconds=20):
        self.enabled = enabled
        self.base_url = (base_url or "").rstrip("/")
        self.auth_token = auth_token or ""
        self.model = model or ""
        self.timeout_seconds = timeout_seconds

    def available(self):
        return bool(self.enabled and self.base_url and self.auth_token and self.model)

    def create_plan(self, text, conversation_id, user_id):
        if not self.available():
            raise RuntimeError("Anthropic-compatible planner is not configured")

        payload = {
            "model": self.model,
            "max_tokens": 1200,
            "temperature": 0,
            "system": "你只返回 JSON 对象，不返回 Markdown。",
            "messages": [
                {
                    "role": "user",
                    "content": build_planner_prompt(text, conversation_id, user_id),
                }
            ],
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urlrequest.Request(
            f"{self.base_url}/v1/messages",
            data=data,
            headers={
                "x-api-key": self.auth_token,
                "Authorization": f"Bearer {self.auth_token}",
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        with urlrequest.urlopen(req, timeout=self.timeout_seconds) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        result = json.loads(body)
        content = result.get("content") or []
        if isinstance(content, list):
            text_parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") in {"text", None}
            ]
            output = "\n".join(part for part in text_parts if part)
        else:
            output = str(content)
        return self._normalize_plan(self._parse_json_text(output))

    def create_recovery_plan(self, context):
        if not self.available():
            raise RuntimeError("Anthropic-compatible planner is not configured")
        payload = {
            "model": self.model,
            "max_tokens": 1200,
            "temperature": 0,
            "system": "你只返回 JSON 对象，不返回 Markdown。",
            "messages": [{"role": "user", "content": build_recovery_prompt(context)}],
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urlrequest.Request(
            f"{self.base_url}/v1/messages",
            data=data,
            headers={
                "x-api-key": self.auth_token,
                "Authorization": f"Bearer {self.auth_token}",
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        with urlrequest.urlopen(req, timeout=self.timeout_seconds) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        result = json.loads(body)
        content = result.get("content") or []
        if isinstance(content, list):
            output = "\n".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") in {"text", None}
            )
        else:
            output = str(content)
        return self._normalize_recovery_plan(self._parse_json_text(output))

    def _parse_json_text(self, text):
        text = (text or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
        return json.loads(text)

    def _normalize_plan(self, plan):
        plan.setdefault("plan_type", "chat")
        plan.setdefault("intent", "chat")
        plan.setdefault("confidence", 0.0)
        plan.setdefault("requires_confirmation", False)
        plan.setdefault("reply", "")
        plan.setdefault("case_ids", [])
        plan.setdefault("stages", [])
        plan.setdefault("batch_size", 2000)
        plan.setdefault("service", "")
        plan.setdefault("keyword", "")
        plan.setdefault("level", "error")
        plan.setdefault("minutes", 30)
        plan.setdefault("workflow_id", "")
        return plan

    def _normalize_recovery_plan(self, plan):
        allowed = {"wait_and_retry", "clear_stage_and_resubmit", "mark_failed"}
        plan.setdefault("diagnosis", "")
        plan.setdefault("action", "mark_failed")
        plan.setdefault("stage", "")
        plan.setdefault("delay_seconds", 0)
        plan.setdefault("requires_approval", False)
        plan.setdefault("risk", "medium")
        plan.setdefault("reason", plan.get("diagnosis") or "AI recovery planner requested action.")
        if plan["action"] not in allowed:
            plan["action"] = "mark_failed"
        if plan["action"] == "clear_stage_and_resubmit":
            plan["requires_approval"] = True
            if plan["risk"] == "low":
                plan["risk"] = "medium"
        return plan


def run_command_with_tree_timeout(cmd, cwd, timeout_seconds):
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
        return {"returncode": proc.returncode, "stdout": stdout, "stderr": stderr}
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
            )
        else:
            proc.kill()
        stdout, stderr = proc.communicate(timeout=5)
        return {
            "returncode": 124,
            "stdout": stdout or "",
            "stderr": (stderr or "") + f"\ncommand timed out after {timeout_seconds}s",
        }


class WorkflowExecutor(threading.Thread):
    def __init__(self, store, tools, notifier, check_interval_seconds, recovery_planner=None):
        super().__init__(daemon=True)
        self.store = store
        self.tools = tools
        self.notifier = notifier
        self.check_interval_seconds = check_interval_seconds
        self.recovery_planner = recovery_planner
        self.stop_event = threading.Event()

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:
                print(f"[workflow][error] {exc}", flush=True)
            self.stop_event.wait(2)

    def tick(self):
        workflows = self.store.query_all(
            "select * from workflows where status in ('queued', 'running') order by created_at"
        )
        for workflow in workflows:
            self.advance_workflow(workflow)

    def advance_workflow(self, workflow):
        workflow_id = workflow["id"]
        conversation_id = workflow["conversation_id"]
        stages = workflow_stages_for(workflow["skill"])
        if not stages:
            self.store.execute(
                "update workflows set status = 'failed', error = ?, updated_at = ? where id = ?",
                (f"unsupported skill: {workflow['skill']}", now_iso(), workflow_id),
            )
            self.notifier.send(conversation_id, f"任务 {workflow_id} 失败：不支持的 skill {workflow['skill']}。", workflow_id)
            return

        if workflow["cancel_requested"]:
            self.store.execute(
                "update workflows set status = 'cancelled', updated_at = ? where id = ?",
                (now_iso(), workflow_id),
            )
            self.notifier.send(conversation_id, f"任务 {workflow_id} 已取消。", workflow_id)
            return

        stage_index = int(workflow["current_stage_index"])
        if stage_index >= len(stages):
            if workflow["status"] != "succeeded":
                self.store.execute(
                    "update workflows set status = 'succeeded', updated_at = ? where id = ?",
                    (now_iso(), workflow_id),
                )
                label = (workflow_template(workflow["skill"]) or {}).get("label", workflow["skill"])
                self.notifier.send(conversation_id, f"{label}任务 {workflow_id} 已全部完成。", workflow_id)
            return

        stage = stages[stage_index]
        stage_row = self.store.query_one(
            "select * from workflow_stages where workflow_id = ? and stage = ?",
            (workflow_id, stage),
        )

        if stage_row is None:
            try:
                self.submit_stage(workflow, stage)
            except Exception as exc:
                self.handle_stage_failure(workflow, stage, exc)
            return

        if stage_row["status"] == "waiting_approval":
            approval = self.store.query_one(
                """
                select * from approvals
                where workflow_id = ? and status = 'pending'
                order by created_at desc limit 1
                """,
                (workflow_id,),
            )
            if approval is not None:
                return
            self.store.execute(
                "delete from workflow_stages where workflow_id = ? and stage = ?",
                (workflow_id, stage),
            )
            self.store.execute(
                "update workflows set status = 'running', updated_at = ? where id = ?",
                (now_iso(), workflow_id),
            )
            return

        if stage_row["status"] == "succeeded":
            next_index = stage_index + 1
            next_status = "succeeded" if next_index >= len(stages) else "running"
            self.store.execute(
                "update workflows set current_stage_index = ?, status = ?, updated_at = ? where id = ?",
                (next_index, next_status, now_iso(), workflow_id),
            )
            if next_status == "succeeded":
                label = (workflow_template(workflow["skill"]) or {}).get("label", workflow["skill"])
                self.notifier.send(conversation_id, f"{label}任务 {workflow_id} 已全部完成。", workflow_id)
            return

        if stage_row["status"] == "running":
            max_attempts = int(stage_row["max_attempts"] or 0)
            if max_attempts and int(stage_row["attempt_count"] or 0) >= max_attempts:
                self.handle_stage_failure(
                    workflow,
                    stage,
                    RuntimeError(f"stage {stage} exceeded max attempts {max_attempts}"),
                    stage_row,
                    {"status": "failed", "error": f"stage exceeded max attempts {max_attempts}"},
                )
                return
            timeout_at = stage_row["timeout_at"]
            if timeout_at and datetime.fromisoformat(timeout_at).timestamp() <= time.time():
                self.handle_stage_failure(
                    workflow,
                    stage,
                    RuntimeError(f"stage {stage} timed out"),
                    stage_row,
                    {"status": "failed", "error": f"stage timed out at {timeout_at}"},
                )
                return
            next_check_at = stage_row["next_check_at"]
            if next_check_at and datetime.fromisoformat(next_check_at).timestamp() > time.time():
                return
            if not next_check_at:
                last_checked = stage_row["last_checked_at"]
                if last_checked:
                    last_ts = datetime.fromisoformat(last_checked).timestamp()
                    if time.time() - last_ts < self.check_interval_seconds:
                        return

            try:
                status = self.check_stage(workflow, stage, stage_row)
            except Exception as exc:
                self.handle_stage_failure(workflow, stage, exc, stage_row)
                return

            if status["status"] == "failed":
                self.handle_stage_failure(
                    workflow,
                    stage,
                    RuntimeError(status.get("error") or f"stage {stage} failed"),
                    stage_row,
                    status,
                )
                return

            if status["status"] == "succeeded":
                self.advance_after_stage_success(workflow, stage, status, stages, stage_index)
            return

    def submit_stage(self, workflow, stage):
        stage_config = workflow_stage_config(workflow["skill"], stage)
        if stage_config is None:
            raise ValueError(f"unsupported stage: {workflow['skill']}/{stage}")
        result = self.execute_stage_with_audit(workflow, stage, stage_config, None)
        created = now_iso()
        status = result.get("status") or "succeeded"
        executor = stage_config.get("executor") or {}
        self.store.execute(
            """
            insert into workflow_stages(
                workflow_id, stage, status, job_id, submitted_count, pending_count,
                success_count, failed_count, last_checked_at, created_at, updated_at,
                executor_json, next_check_at, attempt_count, max_attempts, timeout_at, result_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workflow["id"],
                stage,
                status,
                result.get("job_id") or new_id("job"),
                int(result.get("submitted_count") or 1),
                int(result.get("pending_count") or 0),
                int(result.get("success_count") or (1 if status == "succeeded" else 0)),
                int(result.get("failed_count") or 0),
                created,
                created,
                created,
                json.dumps(executor, ensure_ascii=False),
                self.next_check_at(result, stage_config, created) if status == "running" else None,
                1,
                int(executor.get("max_attempts") or result.get("max_attempts") or 0),
                self.timeout_at(result, stage_config, created) if status == "running" else None,
                json.dumps(result, ensure_ascii=False),
            ),
        )
        stages = workflow_stages_for(workflow["skill"])
        if status == "succeeded" and stage in stages:
            next_index = stages.index(stage) + 1
            workflow_status = "succeeded" if next_index >= len(stages) else "running"
        else:
            next_index = int(workflow["current_stage_index"])
            workflow_status = "running"
        self.store.execute(
            "update workflows set current_stage_index = ?, status = ?, updated_at = ? where id = ?",
            (next_index, workflow_status, created, workflow["id"]),
        )
        message = result.get("message")
        if message:
            self.notifier.send(workflow["conversation_id"], message, workflow["id"])
        if status == "failed":
            raise RuntimeError(result.get("error") or f"stage {stage} failed")
        if workflow_status == "succeeded":
            label = (workflow_template(workflow["skill"]) or {}).get("label", workflow["skill"])
            self.notifier.send(workflow["conversation_id"], f"{label}任务 {workflow['id']} 已全部完成。", workflow["id"])

    def check_stage(self, workflow, stage, stage_row):
        stage_config = workflow_stage_config(workflow["skill"], stage)
        if stage_config is None:
            raise ValueError(f"unsupported stage: {workflow['skill']}/{stage}")
        result = self.execute_stage_with_audit(workflow, stage, stage_config, stage_row)
        status = result.get("status") or "succeeded"
        checked_at = now_iso()
        attempt_count = int(stage_row["attempt_count"] or 0) + 1
        next_check_at = self.next_check_at(result, stage_config, checked_at) if status == "running" else None
        timeout_at = stage_row["timeout_at"] or (self.timeout_at(result, stage_config, checked_at) if status == "running" else None)
        self.store.execute(
            """
            update workflow_stages
            set status = ?, job_id = ?, submitted_count = ?, pending_count = ?,
                success_count = ?, failed_count = ?, last_checked_at = ?, updated_at = ?,
                next_check_at = ?, attempt_count = ?, timeout_at = ?, result_json = ?
            where workflow_id = ? and stage = ?
            """,
            (
                status,
                result.get("job_id") or stage_row["job_id"],
                int(result.get("submitted_count") or stage_row["submitted_count"] or 1),
                int(result.get("pending_count") or 0),
                int(result.get("success_count") or (1 if status == "succeeded" else 0)),
                int(result.get("failed_count") or 0),
                checked_at,
                checked_at,
                next_check_at,
                attempt_count,
                timeout_at,
                json.dumps(result, ensure_ascii=False),
                workflow["id"],
                stage,
            ),
        )
        message = result.get("message")
        if message:
            self.notifier.send(workflow["conversation_id"], message, workflow["id"])
        return result

    def execute_stage_with_audit(self, workflow, stage, stage_config, stage_row):
        run_id = new_id("run")
        started = now_iso()
        attempt_no = int(stage_row["attempt_count"] or 0) + 1 if stage_row is not None else 1
        input_json = json.dumps(
            {
                "workflow_id": workflow["id"],
                "skill": workflow["skill"],
                "stage": stage,
                "stage_config": stage_config,
                "stage_state": dict(stage_row) if stage_row is not None else None,
            },
            ensure_ascii=False,
        )
        self.store.execute(
            """
            insert into workflow_stage_runs(
                id, workflow_id, stage, attempt_no, status, input_json, started_at
            ) values (?, ?, ?, ?, 'running', ?, ?)
            """,
            (run_id, workflow["id"], stage, attempt_no, input_json, started),
        )
        try:
            result = self.tools.execute_stage(workflow, stage, stage_config, stage_row)
            finished = now_iso()
            status = result.get("status") or "succeeded"
            self.store.execute(
                """
                update workflow_stage_runs
                set status = ?, output_json = ?, finished_at = ?
                where id = ?
                """,
                (status, json.dumps(result, ensure_ascii=False), finished, run_id),
            )
            return result
        except Exception as exc:
            finished = now_iso()
            self.store.execute(
                """
                update workflow_stage_runs
                set status = 'failed', error = ?, finished_at = ?
                where id = ?
                """,
                (str(exc), finished, run_id),
            )
            raise

    def next_check_at(self, result, stage_config, base_time):
        seconds = result.get("next_check_seconds")
        executor = stage_config.get("executor") or {}
        if seconds is None:
            seconds = executor.get("poll_interval_seconds") or stage_config.get("poll_interval_seconds") or self.check_interval_seconds
        base = datetime.fromisoformat(base_time)
        return (base + timedelta(seconds=max(1, int(seconds)))).isoformat(timespec="seconds")

    def timeout_at(self, result, stage_config, base_time):
        if result.get("timeout_at"):
            return result["timeout_at"]
        executor = stage_config.get("executor") or {}
        seconds = (
            result.get("max_wait_seconds")
            or executor.get("max_wait_seconds")
            or executor.get("stage_timeout_seconds")
            or stage_config.get("max_wait_seconds")
        )
        if seconds is None and executor.get("max_wait_minutes") is not None:
            seconds = int(executor["max_wait_minutes"]) * 60
        if seconds is None:
            return None
        base = datetime.fromisoformat(base_time)
        return (base + timedelta(seconds=max(1, int(seconds)))).isoformat(timespec="seconds")

    def advance_after_stage_success(self, workflow, stage, status, stages, stage_index):
        workflow_id = workflow["id"]
        self.notifier.send(
            workflow["conversation_id"],
            f"任务 {workflow_id} 阶段 {stage} 已完成，成功 {status.get('success_count', 1)} 条。",
            workflow_id,
        )
        next_index = stage_index + 1
        next_status = "succeeded" if next_index >= len(stages) else "running"
        self.store.execute(
            "update workflows set current_stage_index = ?, status = ?, updated_at = ? where id = ?",
            (next_index, next_status, now_iso(), workflow_id),
        )
        if next_status == "succeeded":
            label = (workflow_template(workflow["skill"]) or {}).get("label", workflow["skill"])
            self.notifier.send(workflow["conversation_id"], f"{label}任务 {workflow_id} 已全部完成。", workflow_id)

    def handle_stage_failure(self, workflow, stage, exc, stage_row=None, status=None):
        workflow_id = workflow["id"]
        error = str(exc)
        context = self.build_failure_context(workflow, stage, error, stage_row, status)
        append_jsonl(LOG_DIR / "recovery-events.jsonl", {"time": now_iso(), "context": context})

        plan = {
            "diagnosis": "No recovery planner configured.",
            "action": "mark_failed",
            "stage": stage or "",
            "delay_seconds": 0,
            "requires_approval": False,
            "risk": "medium",
            "reason": error,
        }
        if self.recovery_planner and hasattr(self.recovery_planner, "create_recovery_plan"):
            try:
                plan = self.recovery_planner.create_recovery_plan(context)
            except Exception as planner_exc:
                plan["diagnosis"] = f"Recovery planner failed: {planner_exc}"
                plan["reason"] = error

        plan["stage"] = plan.get("stage") or stage or ""
        append_jsonl(
            LOG_DIR / "recovery-events.jsonl",
            {"time": now_iso(), "workflow_id": workflow_id, "stage": stage, "recovery_plan": plan},
        )
        self.apply_recovery_plan(workflow, stage, error, plan)

    def build_failure_context(self, workflow, stage, error, stage_row=None, status=None):
        recent_notifications = self.store.query_all(
            """
            select message, created_at from notifications
            where workflow_id = ?
            order by created_at desc
            limit 5
            """,
            (workflow["id"],),
        )
        stages = self.store.query_all(
            "select * from workflow_stages where workflow_id = ? order by created_at",
            (workflow["id"],),
        )
        return {
            "workflow_id": workflow["id"],
            "skill": workflow["skill"],
            "stage": stage,
            "workflow_status": workflow["status"],
            "case_count": workflow["case_count"],
            "error": error,
            "stage_row": dict(stage_row) if stage_row is not None else None,
            "status": status,
            "stages": [dict(row) for row in stages],
            "recent_notifications": [dict(row) for row in recent_notifications],
        }

    def apply_recovery_plan(self, workflow, stage, error, plan):
        workflow_id = workflow["id"]
        action = plan.get("action")
        target_stage = plan.get("stage") or stage
        reason = plan.get("reason") or plan.get("diagnosis") or error

        if action == "wait_and_retry":
            delay = max(0, int(plan.get("delay_seconds") or 0))
            retry_after = datetime.now(timezone.utc).astimezone() + timedelta(seconds=delay)
            retry_after_text = retry_after.isoformat(timespec="seconds")
            self.store.execute(
                """
                update workflow_stages
                set status = 'running', last_checked_at = ?, updated_at = ?
                where workflow_id = ? and stage = ?
                """,
                (retry_after_text, now_iso(), workflow_id, target_stage),
            )
            self.store.execute(
                "update workflows set status = 'running', error = null, updated_at = ? where id = ?",
                (now_iso(), workflow_id),
            )
            self.notifier.send(
                workflow["conversation_id"],
                f"任务 {workflow_id} 阶段 {target_stage} 失败后已进入 AI 恢复：{plan.get('diagnosis') or reason}。将等待约 {delay} 秒后重试检查。",
                workflow_id,
            )
            return

        if action == "clear_stage_and_resubmit":
            self.create_approval(
                workflow=workflow,
                stage=target_stage,
                action="clear_stage_and_resubmit",
                risk_level=plan.get("risk") or "medium",
                reason=f"AI 恢复建议：{reason}",
                payload={"stage": target_stage, "workflow_id": workflow_id, "recovery": True},
            )
            return

        self.store.execute(
            "update workflows set status = 'failed', error = ?, updated_at = ? where id = ?",
            (f"{error}; AI diagnosis: {plan.get('diagnosis')}", now_iso(), workflow_id),
        )
        if target_stage:
            self.store.execute(
                "update workflow_stages set status = 'failed', failed_count = failed_count + 1, updated_at = ? where workflow_id = ? and stage = ?",
                (now_iso(), workflow_id, target_stage),
            )
        self.notifier.send(
            workflow["conversation_id"],
            f"任务 {workflow_id} 阶段 {target_stage} 失败，AI 判断不可自动恢复：{plan.get('diagnosis') or reason}",
            workflow_id,
        )

    def stage_needs_demo_approval(self, workflow, stage):
        return False

    def create_approval(self, workflow, stage, action, risk_level, reason, payload):
        workflow_id = workflow["id"]
        approval_id = new_id("appr")
        created = now_iso()
        expires = datetime.fromtimestamp(time.time() + 30 * 60, timezone.utc).astimezone().isoformat(timespec="seconds")
        self.store.execute(
            """
            insert into approvals(
                id, workflow_id, conversation_id, requester_user_id, approver_user_id,
                status, action, risk_level, reason, payload_json, expires_at, created_at, updated_at
            ) values (?, ?, ?, ?, null, 'pending', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                approval_id,
                workflow_id,
                workflow["conversation_id"],
                workflow["user_id"],
                action,
                risk_level,
                reason,
                json.dumps(payload, ensure_ascii=False),
                expires,
                created,
                created,
            ),
        )
        self.store.execute(
            """
            insert or replace into workflow_stages(
                workflow_id, stage, status, job_id, submitted_count, pending_count,
                success_count, failed_count, last_checked_at, created_at, updated_at
            ) values (?, ?, 'waiting_approval', null, 0, 0, 0, 0, null, ?, ?)
            """,
            (workflow_id, stage, created, created),
        )
        self.store.execute(
            "update workflows set status = 'waiting_approval', updated_at = ? where id = ?",
            (created, workflow_id),
        )
        self.notifier.send(
            workflow["conversation_id"],
            (
                f"Approval required: {approval_id}\n"
                f"Workflow: {workflow_id}\n"
                f"Stage: {stage}\n"
                f"Action: {action}\n"
                f"Risk: {risk_level}\n"
                f"Reason: {reason}\n\n"
                f"Approve: /approve {approval_id}\n"
                f"Reject: /reject {approval_id}"
            ),
            workflow_id,
        )


class SimulatedConsumer(threading.Thread):
    def __init__(self, tools, batch_size, interval_seconds):
        super().__init__(daemon=True)
        self.tools = tools
        self.batch_size = batch_size
        self.interval_seconds = interval_seconds
        self.stop_event = threading.Event()

    def run(self):
        while not self.stop_event.is_set():
            try:
                consumed = self.tools.simulate_consume(self.batch_size)
                if consumed:
                    print(f"[consumer] consumed {consumed} rows", flush=True)
            except Exception as exc:
                print(f"[consumer][error] {exc}", flush=True)
            self.stop_event.wait(self.interval_seconds)


class Orchestrator:
    def __init__(self, store, artifacts, tools, notifier, claude_client=None):
        self.store = store
        self.artifacts = artifacts
        self.tools = tools
        self.notifier = notifier
        self.claude = claude_client

    def handle_message(self, conversation_id, user_id, text):
        text = text.strip()
        lower_text = text.lower()
        if not text:
            return {"reply": "消息为空。"}

        if text.startswith("/status") or lower_text.startswith("status "):
            workflow_id = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
            return {"reply": self.status_text(workflow_id)}

        if text.startswith("/cancel") or lower_text.startswith("cancel "):
            workflow_id = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
            return {"reply": self.cancel(workflow_id)}

        if text.startswith("/approve") or lower_text.startswith("approve "):
            approval_id = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
            return {"reply": self.approve(approval_id, conversation_id, user_id)}

        if text.startswith("/reject") or lower_text.startswith("reject "):
            approval_id = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
            return {"reply": self.reject(approval_id, conversation_id, user_id)}

        if text.startswith("/approvals") or lower_text == "approvals":
            return {"reply": self.list_approvals(conversation_id)}

        if text.startswith("/approval") or lower_text.startswith("approval "):
            approval_id = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
            return {"reply": self.approval_text(approval_id, conversation_id)}

        if text.startswith("/confirm") or lower_text.startswith("confirm "):
            token = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
            return {"reply": self.confirm(token, conversation_id, user_id)}

        matched_capability = self.match_capability_by_text(text)
        if matched_capability is not None:
            return {"reply": self.start_capability_from_text(matched_capability, conversation_id, user_id, text)}

        if self.claude and self.claude.enabled:
            try:
                plan = self.claude.create_plan(text, conversation_id, user_id)
                plan["_original_text"] = text
                return {"reply": self.execute_plan(plan, conversation_id, user_id)}
            except Exception as exc:
                print(f"[claude-plan][fallback] {exc}", flush=True)

        matched_capability = self.match_capability_by_text(text)
        if matched_capability is not None:
            return {"reply": self.start_capability_from_text(matched_capability, conversation_id, user_id, text)}

        return {
            "reply": (
                "目前 MVP 支持：\n"
                "- 确认：发送 `/confirm <token>`\n"
                "- 状态：发送 `/status <workflow_id>`\n"
                "- 取消：发送 `/cancel <workflow_id>`\n"
                "- 查日志：发送 `查日志 payment error`"
            )
        }

    def match_capability_by_text(self, text):
        lower = text.lower()
        for cap in all_capabilities():
            for trigger in cap.get("triggers") or []:
                trigger_lower = str(trigger).lower()
                if trigger_lower and trigger_lower in lower:
                    return cap
        return None

    def looks_like_logs_request(self, text):
        lower = text.lower()
        return lower.startswith("logs") or "\u65e5\u5fd7" in text or " log" in f" {lower}"

    def execute_plan(self, plan, conversation_id, user_id):
        append_jsonl(
            LOG_DIR / "claude-plans.jsonl",
            {
                "time": now_iso(),
                "conversation_id": conversation_id,
                "user_id": user_id,
                "plan": plan,
            },
        )
        intent = plan.get("intent")
        capability = capability_for_intent(intent)
        if capability is not None:
            payload = self.payload_from_plan(capability, plan)
            return self.start_workflow(
                skill=capability["name"],
                conversation_id=conversation_id,
                user_id=user_id,
                payload=payload,
                item_count=self.item_count_from_payload(payload),
                created_message=capability.get("created_message") or f"已创建{capability.get('label', capability['name'])}任务",
            )

        if intent == "status":
            return self.status_text(plan.get("workflow_id") or "")

        if intent == "cancel":
            return self.cancel(plan.get("workflow_id") or "")

        return plan.get("reply") or "我还不能处理这个请求。"

    def payload_from_plan(self, capability, plan):
        payload = dict(capability.get("input_defaults") or {})
        for key, value in plan.items():
            if key.startswith("_") or value in (None, ""):
                continue
            payload[key] = value
        payload["skill"] = capability["name"]
        return payload

    def item_count_from_payload(self, payload):
        for key in ("case_ids", "items", "ids"):
            value = payload.get(key)
            if isinstance(value, list):
                if value:
                    return len(value)
                continue
        return int(payload.get("item_count") or payload.get("case_count") or 1)

    def start_capability_from_text(self, capability, conversation_id, user_id, text):
        if capability["name"] == "logs-search":
            return self.start_logs_workflow(conversation_id, user_id, text)
        payload = dict(capability.get("input_defaults") or {})
        payload["_original_text"] = text
        payload["skill"] = capability["name"]
        return self.start_workflow(
            skill=capability["name"],
            conversation_id=conversation_id,
            user_id=user_id,
            payload=payload,
            item_count=self.item_count_from_payload(payload),
            created_message=capability.get("created_message") or f"已创建{capability.get('label', capability['name'])}任务",
        )

    def start_logs_workflow(self, conversation_id, user_id, text):
        payload = self.parse_logs_request(text)
        return self.start_workflow(
            skill="logs-search",
            conversation_id=conversation_id,
            user_id=user_id,
            payload=payload,
            item_count=1,
            created_message="已创建日志查询任务",
        )

    def parse_logs_request(self, text):
        parts = text.split()
        service = "default-service"
        keyword = ""
        if parts:
            head = parts[0].lower()
            if head in {"logs", "log", "查日志", "日志"}:
                service = parts[1] if len(parts) > 1 else service
                keyword = " ".join(parts[2:]) if len(parts) > 2 else ""
            else:
                service = parts[0]
                keyword = " ".join(parts[1:]) if len(parts) > 1 else ""
        return {
            "skill": "logs-search",
            "service": service,
            "keyword": keyword,
            "level": "error",
            "minutes": 30,
            "limit": 20,
        }

    def start_workflow(self, skill, conversation_id, user_id, payload, item_count, created_message):
        if workflow_template(skill) is None:
            return f"不支持的 skill：{skill}"
        payload = dict(payload or {})
        payload.setdefault("skill", skill)
        artifact_id = self.artifacts.save_payload(payload)
        workflow_id = self.create_workflow(
            skill=skill,
            conversation_id=conversation_id,
            user_id=user_id,
            artifact_id=artifact_id,
            item_count=item_count,
        )
        self.notifier.send(
            conversation_id,
            f"{created_message} {workflow_id}，后台开始执行。可发送 status {workflow_id} 查看进度。",
            workflow_id,
        )
        return f"{created_message}：{workflow_id}\n查看状态：status {workflow_id}"

    def create_workflow(self, skill, conversation_id, user_id, artifact_id, item_count):
        workflow_id = new_id("wf")
        self.store.execute(
            """
            insert into workflows(
                id, skill, status, conversation_id, user_id, artifact_id, case_count,
                current_stage_index, cancel_requested, created_at, updated_at
            ) values (?, ?, 'queued', ?, ?, ?, ?, 0, 0, ?, ?)
            """,
            (
                workflow_id,
                skill,
                conversation_id,
                user_id,
                artifact_id,
                item_count,
                now_iso(),
                now_iso(),
            ),
        )
        return workflow_id

    def confirm(self, token, conversation_id, user_id):
        row = self.store.query_one("select * from pending_confirmations where token = ?", (token,))
        if row is None:
            return "确认 token 不存在或已使用。"
        if row["conversation_id"] != conversation_id or row["user_id"] != user_id:
            return "确认 token 与当前会话或用户不匹配。"

        payload = self.artifacts.load_payload(row["artifact_id"])
        skill = payload.get("skill")
        if not skill:
            return "确认 token 缺少 skill 信息，无法创建任务。"
        workflow_id = self.create_workflow(
            skill=skill,
            conversation_id=row["conversation_id"],
            user_id=row["user_id"],
            artifact_id=row["artifact_id"],
            item_count=row["case_count"],
        )
        self.store.execute("delete from pending_confirmations where token = ?", (token,))
        label = (workflow_template(skill) or {}).get("label", skill)
        self.notifier.send(
            row["conversation_id"],
            f"已创建{label}任务 {workflow_id}，后台开始执行。可发送 status {workflow_id} 查看进度。",
            workflow_id,
        )
        return f"已创建{label}任务：{workflow_id}\n查看状态：status {workflow_id}"

    def status_text(self, workflow_id):
        if not workflow_id:
            rows = self.store.query_all(
                "select id, skill, status, current_stage_index, case_count, updated_at from workflows order by created_at desc limit 10"
            )
            if not rows:
                return "暂无任务。"
            return "\n".join(
                f"{r['id']} | {r['skill']} | {r['status']} | {r['case_count']} 条 | {r['updated_at']}"
                for r in rows
            )

        workflow = self.store.query_one("select * from workflows where id = ?", (workflow_id,))
        if workflow is None:
            return f"任务不存在：{workflow_id}"
        stages = self.store.query_all(
            "select * from workflow_stages where workflow_id = ? order by created_at",
            (workflow_id,),
        )
        lines = [
            f"任务：{workflow_id}",
            f"状态：{workflow['status']}",
            f"单号数：{workflow['case_count']}",
        ]
        for stage in stages:
            lines.append(
                f"- {stage['stage']}: {stage['status']}, "
                f"pending={stage['pending_count']}, success={stage['success_count']}, failed={stage['failed_count']}"
            )
        return "\n".join(lines)

    def cancel(self, workflow_id):
        if not workflow_id:
            return "请提供 workflow_id。"
        row = self.store.query_one("select id from workflows where id = ?", (workflow_id,))
        if row is None:
            return f"任务不存在：{workflow_id}"
        self.store.execute(
            "update workflows set cancel_requested = 1, updated_at = ? where id = ?",
            (now_iso(), workflow_id),
        )
        return f"已请求取消任务：{workflow_id}"

    def approve(self, approval_id, conversation_id, user_id):
        approval = self.get_pending_approval(approval_id, conversation_id)
        if approval is None:
            return f"Approval not found or not pending: {approval_id}"
        if not self.user_can_approve(user_id, approval):
            return f"User {user_id} is not allowed to approve {approval_id}."

        payload = json.loads(approval["payload_json"] or "{}")
        stage = payload.get("stage")
        now = now_iso()
        self.store.execute(
            """
            update approvals
            set status = 'approved', approver_user_id = ?, updated_at = ?
            where id = ?
            """,
            (user_id, now, approval_id),
        )
        if stage:
            self.store.execute(
                "delete from workflow_stages where workflow_id = ? and stage = ? and status = 'waiting_approval'",
                (approval["workflow_id"], stage),
            )
        self.store.execute(
            "update workflows set status = 'running', updated_at = ? where id = ?",
            (now, approval["workflow_id"]),
        )
        self.notifier.send(
            approval["conversation_id"],
            f"Approval {approval_id} approved by {user_id}. Workflow {approval['workflow_id']} resumed.",
            approval["workflow_id"],
        )
        return f"Approved {approval_id}. Workflow {approval['workflow_id']} will continue."

    def reject(self, approval_id, conversation_id, user_id):
        approval = self.get_pending_approval(approval_id, conversation_id)
        if approval is None:
            return f"Approval not found or not pending: {approval_id}"
        if not self.user_can_approve(user_id, approval):
            return f"User {user_id} is not allowed to reject {approval_id}."

        now = now_iso()
        self.store.execute(
            """
            update approvals
            set status = 'rejected', approver_user_id = ?, updated_at = ?
            where id = ?
            """,
            (user_id, now, approval_id),
        )
        self.store.execute(
            "update workflows set status = 'failed', error = ?, updated_at = ? where id = ?",
            (f"approval rejected: {approval_id}", now, approval["workflow_id"]),
        )
        self.notifier.send(
            approval["conversation_id"],
            f"Approval {approval_id} rejected by {user_id}. Workflow {approval['workflow_id']} stopped.",
            approval["workflow_id"],
        )
        return f"Rejected {approval_id}. Workflow {approval['workflow_id']} stopped."

    def get_pending_approval(self, approval_id, conversation_id):
        if not approval_id:
            return None
        return self.store.query_one(
            """
            select * from approvals
            where id = ? and conversation_id = ? and status = 'pending'
            """,
            (approval_id, conversation_id),
        )

    def user_can_approve(self, user_id, approval):
        raw = os.environ.get("APPROVER_USER_IDS", "*").strip()
        if raw == "*":
            return True
        allowed = {item.strip() for item in raw.split(",") if item.strip()}
        return user_id in allowed or user_id == approval["requester_user_id"]

    def list_approvals(self, conversation_id):
        rows = self.store.query_all(
            """
            select id, workflow_id, status, action, risk_level, created_at
            from approvals
            where conversation_id = ?
            order by created_at desc limit 20
            """,
            (conversation_id,),
        )
        if not rows:
            return "No approvals."
        return "\n".join(
            f"{r['id']} | {r['status']} | {r['risk_level']} | {r['action']} | {r['workflow_id']}"
            for r in rows
        )

    def approval_text(self, approval_id, conversation_id):
        row = self.store.query_one(
            "select * from approvals where id = ? and conversation_id = ?",
            (approval_id, conversation_id),
        )
        if row is None:
            return f"Approval not found: {approval_id}"
        return (
            f"Approval: {row['id']}\n"
            f"Workflow: {row['workflow_id']}\n"
            f"Status: {row['status']}\n"
            f"Action: {row['action']}\n"
            f"Risk: {row['risk_level']}\n"
            f"Reason: {row['reason']}\n"
            f"Approve: /approve {row['id']}\n"
            f"Reject: /reject {row['id']}"
        )

    def handle_logs(self, text):
        parts = text.split()
        service = parts[1] if len(parts) > 1 else "default-service"
        keyword = parts[2] if len(parts) > 2 else ""
        result = self.tools.logs_search(service=service, keyword=keyword)
        item = result["items"][0]
        return (
            f"日志查询结果：\n"
            f"服务：{result['service']}\n"
            f"级别：{result['level']}\n"
            f"样例：{item['timestamp']} {item['message']}"
        )


def clean_case_ids(values):
    seen = set()
    result = []
    for item in values:
        value = str(item).strip()
        if not re.match(r"^[A-Za-z0-9_-]{3,64}$", value):
            continue
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def render_dashboard(store):
    workflows = store.query_all(
        """
        select * from workflows
        order by created_at desc
        limit 30
        """
    )
    approvals = store.query_all(
        """
        select * from approvals
        order by created_at desc
        limit 30
        """
    )
    notifications = store.query_all(
        """
        select * from notifications
        order by created_at desc
        limit 30
        """
    )
    stages = store.query_all(
        """
        select * from workflow_stages
        order by updated_at desc
        limit 120
        """
    )

    stage_map = {}
    for stage in stages:
        stage_map.setdefault(stage["workflow_id"], []).append(stage)

    status_counts = store.query_all(
        "select status, count(*) as count from workflows group by status order by status"
    )
    approval_counts = store.query_all(
        "select status, count(*) as count from approvals group by status order by status"
    )

    def badge(text):
        cls = "neutral"
        if text in {"succeeded", "approved"}:
            cls = "good"
        elif text in {"failed", "rejected", "cancelled", "timeout"}:
            cls = "bad"
        elif text in {"running", "queued"}:
            cls = "run"
        elif text in {"waiting_approval", "pending"}:
            cls = "wait"
        return f'<span class="badge {cls}">{escape(str(text))}</span>'

    workflow_rows = []
    for wf in workflows:
        wf_stages = stage_map.get(wf["id"], [])
        stage_html = "".join(
            f"""
            <div class="stage">
              <div><b>{escape(s['stage'])}</b> {badge(s['status'])}</div>
              <div class="muted">pending {s['pending_count']} · success {s['success_count']} · failed {s['failed_count']}</div>
            </div>
            """
            for s in wf_stages
        )
        if not stage_html:
            stage_html = '<div class="muted">No stages yet</div>'
        workflow_rows.append(
            f"""
            <section class="card">
              <div class="row">
                <div>
                  <h3>{escape(wf['id'])}</h3>
                  <div class="muted">{escape(wf['skill'])} · {wf['case_count']} cases · stage index {wf['current_stage_index']}</div>
                  <div class="muted mono">{escape(wf['conversation_id'])}</div>
                </div>
                <div>{badge(wf['status'])}</div>
              </div>
              <div class="stages">{stage_html}</div>
              <div class="muted">created {escape(wf['created_at'])} · updated {escape(wf['updated_at'])}</div>
              <div class="commands">
                <code>status {escape(wf['id'])}</code>
                <code>cancel {escape(wf['id'])}</code>
              </div>
            </section>
            """
        )

    approval_rows = []
    for appr in approvals:
        approval_rows.append(
            f"""
            <section class="card compact">
              <div class="row">
                <div>
                  <h3>{escape(appr['id'])}</h3>
                  <div class="muted">workflow {escape(appr['workflow_id'])}</div>
                  <div>{escape(appr['action'])} · {escape(appr['risk_level'])}</div>
                </div>
                <div>{badge(appr['status'])}</div>
              </div>
              <p>{escape(appr['reason'])}</p>
              <div class="commands">
                <code>approval {escape(appr['id'])}</code>
                <code>approve {escape(appr['id'])}</code>
                <code>reject {escape(appr['id'])}</code>
              </div>
            </section>
            """
        )

    notification_rows = []
    for ntf in notifications:
        notification_rows.append(
            f"""
            <div class="notification">
              <div class="muted">{escape(ntf['created_at'])} · {escape(ntf['workflow_id'] or '-')}</div>
              <pre>{escape(ntf['message'])}</pre>
            </div>
            """
        )

    def count_pills(rows):
        if not rows:
            return '<span class="pill">none 0</span>'
        return "".join(f'<span class="pill">{escape(r["status"])} {r["count"]}</span>' for r in rows)

    return f"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="10">
  <title>DingTalk Claude Agent Dashboard</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #1f2937;
      --muted: #667085;
      --line: #d9dee7;
      --good: #0f7b45;
      --bad: #b42318;
      --run: #175cd3;
      --wait: #b54708;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    header {{
      padding: 20px 28px;
      background: #111827;
      color: white;
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
    }}
    header h1 {{ margin: 0; font-size: 20px; font-weight: 650; }}
    header .muted {{ color: #cbd5e1; }}
    main {{ padding: 20px 28px 40px; }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }}
    .panel, .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 1px 2px rgba(16, 24, 40, .04);
    }}
    .panel {{ padding: 14px; }}
    .layout {{
      display: grid;
      grid-template-columns: minmax(0, 1.4fr) minmax(320px, .8fr);
      gap: 18px;
    }}
    h2 {{ margin: 0 0 12px; font-size: 16px; }}
    h3 {{ margin: 0 0 4px; font-size: 15px; }}
    .card {{ padding: 14px; margin-bottom: 12px; }}
    .compact p {{ margin: 10px 0; line-height: 1.45; }}
    .row {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; }}
    .muted {{ color: var(--muted); font-size: 12px; }}
    .mono {{ font-family: Consolas, monospace; word-break: break-all; }}
    .badge {{
      display: inline-block;
      border-radius: 999px;
      padding: 3px 8px;
      font-size: 12px;
      font-weight: 600;
      border: 1px solid var(--line);
      white-space: nowrap;
    }}
    .badge.good {{ color: var(--good); background: #ecfdf3; border-color: #abefc6; }}
    .badge.bad {{ color: var(--bad); background: #fef3f2; border-color: #fecdca; }}
    .badge.run {{ color: var(--run); background: #eff8ff; border-color: #b2ddff; }}
    .badge.wait {{ color: var(--wait); background: #fffaeb; border-color: #fedf89; }}
    .badge.neutral {{ color: #344054; background: #f2f4f7; }}
    .pill {{
      display: inline-block;
      margin: 4px 6px 0 0;
      padding: 5px 8px;
      border-radius: 6px;
      background: #eef2f6;
      font-size: 12px;
    }}
    .stages {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
      gap: 8px;
      margin: 12px 0;
    }}
    .stage {{
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fbfcfe;
    }}
    .commands {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }}
    code {{
      background: #f2f4f7;
      border: 1px solid #e4e7ec;
      border-radius: 5px;
      padding: 3px 6px;
      font-family: Consolas, monospace;
      font-size: 12px;
    }}
    .notification {{
      border-top: 1px solid var(--line);
      padding: 10px 0;
    }}
    .notification:first-child {{ border-top: 0; padding-top: 0; }}
    pre {{
      margin: 6px 0 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: Consolas, monospace;
      font-size: 12px;
      line-height: 1.45;
    }}
    a {{ color: #175cd3; text-decoration: none; }}
    @media (max-width: 920px) {{
      .layout {{ grid-template-columns: 1fr; }}
      header {{ align-items: flex-start; flex-direction: column; }}
    }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>DingTalk Claude Agent Dashboard</h1>
      <div class="muted">Auto refreshes every 10 seconds · {escape(now_iso())}</div>
    </div>
    <div><a href="/dashboard" style="color:white">Refresh</a></div>
  </header>
  <main>
    <section class="summary">
      <div class="panel"><h2>Workflows</h2>{count_pills(status_counts)}</div>
      <div class="panel"><h2>Approvals</h2>{count_pills(approval_counts)}</div>
      <div class="panel"><h2>Service</h2><span class="pill">Orchestrator online</span><span class="pill">SQLite backend</span></div>
    </section>
    <section class="layout">
      <div>
        <h2>Recent Workflows</h2>
        {''.join(workflow_rows) or '<div class="panel muted">No workflows</div>'}
      </div>
      <aside>
        <h2>Approvals</h2>
        {''.join(approval_rows) or '<div class="panel muted">No approvals</div>'}
        <h2 style="margin-top:18px">Notifications</h2>
        <div class="panel">{''.join(notification_rows) or '<div class="muted">No notifications</div>'}</div>
      </aside>
    </section>
  </main>
</body>
</html>
"""


def render_dashboard_zh(store):
    workflows = store.query_all(
        """
        select * from workflows
        order by created_at desc
        limit 30
        """
    )
    approvals = store.query_all(
        """
        select * from approvals
        order by created_at desc
        limit 30
        """
    )
    notifications = store.query_all(
        """
        select * from notifications
        order by created_at desc
        limit 30
        """
    )
    stages = store.query_all(
        """
        select * from workflow_stages
        order by updated_at desc
        limit 120
        """
    )

    stage_map = {}
    for stage in stages:
        stage_map.setdefault(stage["workflow_id"], []).append(stage)

    status_counts = store.query_all(
        "select status, count(*) as count from workflows group by status order by status"
    )
    approval_counts = store.query_all(
        "select status, count(*) as count from approvals group by status order by status"
    )

    status_labels = {
        "queued": "排队中",
        "running": "执行中",
        "waiting_approval": "等待审批",
        "succeeded": "已完成",
        "failed": "失败",
        "cancelled": "已取消",
        "timeout": "超时",
        "pending": "待审批",
        "approved": "已通过",
        "rejected": "已拒绝",
    }
    stage_labels = {}
    for cap in all_capabilities():
        for stage in cap.get("stages") or []:
            stage_labels[stage["name"]] = stage.get("label") or stage["name"]
    skill_labels = {
        "status": "状态查询",
        "cancel": "取消任务",
    }
    for cap in all_capabilities():
        skill_labels[cap["name"]] = cap.get("label") or cap["name"]
        for alias in cap.get("aliases") or []:
            skill_labels[alias] = cap.get("label") or cap["name"]
    action_labels = {
        "clear_stage_and_resubmit": "清理当前阶段并重新提交",
    }
    risk_labels = {
        "low": "低风险",
        "medium": "中风险",
        "high": "高风险",
    }

    def label(mapping, value):
        value = "" if value is None else str(value)
        return mapping.get(value, value)

    def badge(text):
        cls = "neutral"
        if text in {"succeeded", "approved"}:
            cls = "good"
        elif text in {"failed", "rejected", "cancelled", "timeout"}:
            cls = "bad"
        elif text in {"running", "queued"}:
            cls = "run"
        elif text in {"waiting_approval", "pending"}:
            cls = "wait"
        return f'<span class="badge {cls}">{escape(label(status_labels, text))}</span>'

    workflow_rows = []
    for wf in workflows:
        wf_stages = stage_map.get(wf["id"], [])
        stage_html = "".join(
            f"""
            <div class="stage">
              <div><b>{escape(label(stage_labels, s['stage']))}</b> {badge(s['status'])}</div>
              <div class="muted mono">{escape(s['stage'])}</div>
              <div class="muted">待消费 {s['pending_count']} 条 / 成功 {s['success_count']} 条 / 失败 {s['failed_count']} 条</div>
            </div>
            """
            for s in wf_stages
        )
        if not stage_html:
            stage_html = '<div class="muted">还没有阶段记录</div>'
        workflow_rows.append(
            f"""
            <section class="card">
              <div class="row">
                <div>
                  <h3>{escape(wf['id'])}</h3>
                  <div class="muted">{escape(label(skill_labels, wf['skill']))} / 单号 {wf['case_count']} 个 / 当前阶段序号 {wf['current_stage_index']}</div>
                  <div class="muted mono">skill: {escape(wf['skill'])}</div>
                  <div class="muted mono">{escape(wf['conversation_id'])}</div>
                </div>
                <div>{badge(wf['status'])}</div>
              </div>
              <div class="stages">{stage_html}</div>
              <div class="muted">创建时间 {escape(wf['created_at'])} / 更新时间 {escape(wf['updated_at'])}</div>
              <div class="commands">
                <code>status {escape(wf['id'])}</code>
                <code>cancel {escape(wf['id'])}</code>
              </div>
            </section>
            """
        )

    approval_rows = []
    for appr in approvals:
        approval_rows.append(
            f"""
            <section class="card compact">
              <div class="row">
                <div>
                  <h3>{escape(appr['id'])}</h3>
                  <div class="muted">任务 {escape(appr['workflow_id'])}</div>
                  <div>{escape(label(action_labels, appr['action']))} / {escape(label(risk_labels, appr['risk_level']))}</div>
                </div>
                <div>{badge(appr['status'])}</div>
              </div>
              <p>{escape(appr['reason'])}</p>
              <div class="muted">创建时间 {escape(appr['created_at'])}</div>
              <div class="commands">
                <code>approval {escape(appr['id'])}</code>
                <code>approve {escape(appr['id'])}</code>
                <code>reject {escape(appr['id'])}</code>
              </div>
            </section>
            """
        )

    notification_rows = []
    for ntf in notifications:
        notification_rows.append(
            f"""
            <div class="notification">
              <div class="muted">{escape(ntf['created_at'])} / 任务 {escape(ntf['workflow_id'] or '-')}</div>
              <pre>{escape(ntf['message'])}</pre>
            </div>
            """
        )

    def count_pills(rows):
        if not rows:
            return '<span class="pill">暂无 0</span>'
        return "".join(
            f'<span class="pill">{escape(label(status_labels, r["status"]))} {r["count"]}</span>'
            for r in rows
        )

    return f"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="10">
  <title>钉钉 Claude 任务面板</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #1f2937;
      --muted: #667085;
      --line: #d9dee7;
      --good: #0f7b45;
      --bad: #b42318;
      --run: #175cd3;
      --wait: #b54708;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    header {{
      padding: 20px 28px;
      background: #111827;
      color: white;
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
    }}
    header h1 {{ margin: 0; font-size: 20px; font-weight: 650; }}
    header .muted {{ color: #cbd5e1; }}
    main {{ padding: 20px 28px 40px; }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }}
    .panel, .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 1px 2px rgba(16, 24, 40, .04);
    }}
    .panel {{ padding: 14px; }}
    .layout {{
      display: grid;
      grid-template-columns: minmax(0, 1.4fr) minmax(320px, .8fr);
      gap: 18px;
    }}
    h2 {{ margin: 0 0 12px; font-size: 16px; }}
    h3 {{ margin: 0 0 4px; font-size: 15px; }}
    .card {{ padding: 14px; margin-bottom: 12px; }}
    .compact p {{ margin: 10px 0; line-height: 1.45; }}
    .row {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; }}
    .muted {{ color: var(--muted); font-size: 12px; }}
    .mono {{ font-family: Consolas, monospace; word-break: break-all; }}
    .badge {{
      display: inline-block;
      border-radius: 999px;
      padding: 3px 8px;
      font-size: 12px;
      font-weight: 600;
      border: 1px solid var(--line);
      white-space: nowrap;
    }}
    .badge.good {{ color: var(--good); background: #ecfdf3; border-color: #abefc6; }}
    .badge.bad {{ color: var(--bad); background: #fef3f2; border-color: #fecdca; }}
    .badge.run {{ color: var(--run); background: #eff8ff; border-color: #b2ddff; }}
    .badge.wait {{ color: var(--wait); background: #fffaeb; border-color: #fedf89; }}
    .badge.neutral {{ color: #344054; background: #f2f4f7; }}
    .pill {{
      display: inline-block;
      margin: 4px 6px 0 0;
      padding: 5px 8px;
      border-radius: 6px;
      background: #eef2f6;
      font-size: 12px;
    }}
    .stages {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
      gap: 8px;
      margin: 12px 0;
    }}
    .stage {{
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fbfcfe;
    }}
    .commands {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }}
    code {{
      background: #f2f4f7;
      border: 1px solid #e4e7ec;
      border-radius: 5px;
      padding: 3px 6px;
      font-family: Consolas, monospace;
      font-size: 12px;
    }}
    .notification {{
      border-top: 1px solid var(--line);
      padding: 10px 0;
    }}
    .notification:first-child {{ border-top: 0; padding-top: 0; }}
    pre {{
      margin: 6px 0 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: Consolas, monospace;
      font-size: 12px;
      line-height: 1.45;
    }}
    a {{ color: #175cd3; text-decoration: none; }}
    @media (max-width: 920px) {{
      .layout {{ grid-template-columns: 1fr; }}
      header {{ align-items: flex-start; flex-direction: column; }}
    }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>钉钉 Claude 任务面板</h1>
      <div class="muted">每 10 秒自动刷新 / {escape(now_iso())}</div>
    </div>
    <div><a href="/dashboard" style="color:white">刷新</a></div>
  </header>
  <main>
    <section class="summary">
      <div class="panel"><h2>任务状态</h2>{count_pills(status_counts)}</div>
      <div class="panel"><h2>审批状态</h2>{count_pills(approval_counts)}</div>
      <div class="panel"><h2>服务状态</h2><span class="pill">调度器在线</span><span class="pill">SQLite 存储</span></div>
    </section>
    <section class="layout">
      <div>
        <h2>最近任务</h2>
        {''.join(workflow_rows) or '<div class="panel muted">暂无任务</div>'}
      </div>
      <aside>
        <h2>审批</h2>
        {''.join(approval_rows) or '<div class="panel muted">暂无审批</div>'}
        <h2 style="margin-top:18px">通知记录</h2>
        <div class="panel">{''.join(notification_rows) or '<div class="muted">暂无通知</div>'}</div>
      </aside>
    </section>
  </main>
</body>
</html>
"""


class ApiHandler(BaseHTTPRequestHandler):
    orchestrator = None
    store = None

    def log_message(self, fmt, *args):
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/dashboard"}:
            html_response(self, 200, render_dashboard_zh(self.store))
            return
        if parsed.path == "/health":
            json_response(self, 200, {"status": "ok", "time": now_iso()})
            return
        if parsed.path == "/api/capabilities":
            items = []
            for cap in all_capabilities():
                items.append(
                    {
                        "name": cap["name"],
                        "label": cap.get("label", cap["name"]),
                        "intent": cap.get("intent", cap["name"]),
                        "aliases": cap.get("aliases") or [],
                        "triggers": cap.get("triggers") or [],
                        "stages": [
                            {
                                "name": stage["name"],
                                "label": stage.get("label", stage["name"]),
                                "executor": stage.get("executor", {}),
                            }
                            for stage in cap.get("stages") or []
                        ],
                    }
                )
            json_response(self, 200, {"items": items})
            return
        if parsed.path == "/api/atomics":
            rows = self.store.query_all(
                """
                select name, label, type, risk, requires_approval, enabled,
                       description, source, updated_at
                from atomic_capabilities
                order by name
                """
            )
            json_response(self, 200, {"items": [dict(row) for row in rows]})
            return
        if parsed.path == "/api/workflows":
            rows = self.store.query_all("select * from workflows order by created_at desc limit 50")
            json_response(self, 200, {"items": [dict(row) for row in rows]})
            return
        if parsed.path.startswith("/api/workflows/"):
            workflow_id = parsed.path.rsplit("/", 1)[-1]
            workflow = self.store.query_one("select * from workflows where id = ?", (workflow_id,))
            if workflow is None:
                json_response(self, 404, {"error": "workflow not found"})
                return
            stages = self.store.query_all(
                "select * from workflow_stages where workflow_id = ? order by created_at",
                (workflow_id,),
            )
            json_response(
                self,
                200,
                {"workflow": dict(workflow), "stages": [dict(stage) for stage in stages]},
            )
            return
        if parsed.path == "/api/notifications":
            query = parse_qs(parsed.query)
            workflow_id = query.get("workflow_id", [""])[0]
            conversation_id = query.get("conversation_id", [""])[0]
            delivery_status = query.get("delivery_status", ["all"])[0] or "all"
            clauses = []
            params = []
            if workflow_id:
                clauses.append("workflow_id = ?")
                params.append(workflow_id)
            if conversation_id:
                clauses.append("conversation_id = ?")
                params.append(conversation_id)
            if delivery_status != "all":
                clauses.append("status = ?")
                params.append(delivery_status)
            where_sql = f" where {' and '.join(clauses)}" if clauses else ""
            rows = self.store.query_all(
                f"select * from notifications{where_sql} order by created_at desc limit 50",
                tuple(params),
            )
            json_response(self, 200, {"items": [dict(row) for row in rows]})
            return
        json_response(self, 404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/messages":
            body = read_json(self)
            response = self.orchestrator.handle_message(
                conversation_id=body.get("conversation_id", "local"),
                user_id=body.get("user_id", "user"),
                text=body.get("text", ""),
            )
            json_response(self, 200, response)
            return
        if parsed.path == "/api/admin/reload-capabilities":
            try:
                loaded = reload_capabilities()
                atomics = self.store.reload_atomics()
                names = sorted({cap["name"] for cap in loaded.values()})
                atomic_names = sorted({item["name"] for item in atomics})
                json_response(
                    self,
                    200,
                    {"status": "ok", "capabilities": names, "atomics": atomic_names},
                )
            except Exception as exc:
                json_response(self, 500, {"status": "error", "error": str(exc)})
            return
        if parsed.path == "/api/admin/reload-atomics":
            try:
                atomics = self.store.reload_atomics()
                atomic_names = sorted({item["name"] for item in atomics})
                json_response(self, 200, {"status": "ok", "atomics": atomic_names})
            except Exception as exc:
                json_response(self, 500, {"status": "error", "error": str(exc)})
            return
        if parsed.path.startswith("/api/notifications/") and parsed.path.endswith("/delivery"):
            notification_id = parsed.path.removeprefix("/api/notifications/").removesuffix("/delivery").strip("/")
            body = read_json(self)
            status = body.get("status")
            if status not in {"sent", "failed"}:
                json_response(self, 400, {"error": "status must be sent or failed"})
                return
            now = now_iso()
            if status == "sent":
                cur = self.store.execute(
                    """
                    update notifications
                    set status = 'sent',
                        delivery_attempts = delivery_attempts + 1,
                        last_error = null,
                        delivered_at = ?,
                        updated_at = ?
                    where id = ?
                    """,
                    (now, now, notification_id),
                )
            else:
                cur = self.store.execute(
                    """
                    update notifications
                    set status = 'failed',
                        delivery_attempts = delivery_attempts + 1,
                        last_error = ?,
                        delivered_at = null,
                        updated_at = ?
                    where id = ?
                    """,
                    (str(body.get("error") or ""), now, notification_id),
                )
            if cur.rowcount == 0:
                json_response(self, 404, {"error": "notification not found"})
                return
            json_response(self, 200, {"status": "ok", "notification_id": notification_id})
            return
        json_response(self, 404, {"error": "not found"})


def main():
    parser = argparse.ArgumentParser(description="DingTalk Claude Agent MVP")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8787, type=int)
    parser.add_argument("--check-interval", default=5, type=int)
    parser.add_argument("--consumer-interval", default=3, type=int)
    parser.add_argument("--consumer-batch-size", default=500, type=int)
    args = parser.parse_args()

    loaded_capabilities = reload_capabilities()
    store = Store(DB_PATH)
    store.init()
    loaded_atomics = store.reload_atomics()
    artifacts = ArtifactStore()
    notifier = NotificationRouter(store)
    tools = ToolGateway(store, artifacts, notifier)
    planner_provider = os.environ.get("PLANNER_PROVIDER", "claude_code").lower()
    planner_timeout = int(os.environ.get("PLANNER_TIMEOUT_SECONDS", os.environ.get("CLAUDE_PLAN_TIMEOUT_SECONDS", "15")))
    if planner_provider in {"anthropic", "anthropic_api"}:
        planner_client = AnthropicCompatiblePlannerClient(
            enabled=True,
            base_url=os.environ.get("ANTHROPIC_BASE_URL", ""),
            auth_token=os.environ.get("ANTHROPIC_AUTH_TOKEN", ""),
            model=os.environ.get("ANTHROPIC_MODEL", ""),
            timeout_seconds=planner_timeout,
        )
    elif planner_provider == "api":
        planner_client = OpenAICompatiblePlannerClient(
            enabled=True,
            base_url=os.environ.get("LLM_API_BASE_URL", ""),
            api_key=os.environ.get("LLM_API_KEY", ""),
            model=os.environ.get("LLM_MODEL", ""),
            timeout_seconds=planner_timeout,
        )
    else:
        claude_enabled = os.environ.get("CLAUDE_PLAN_ENABLED", "1").lower() not in {"0", "false", "no"}
        claude_cmd = os.environ.get("CLAUDE_CMD", "claude")
        claude_model = os.environ.get("CLAUDE_MODEL", "")
        planner_client = ClaudeCodeClient(
            enabled=claude_enabled,
            command=claude_cmd,
            timeout_seconds=planner_timeout,
            model=claude_model,
        )
    orchestrator = Orchestrator(store, artifacts, tools, notifier, planner_client)

    workflow_executor = WorkflowExecutor(store, tools, notifier, args.check_interval, recovery_planner=planner_client)
    consumer = SimulatedConsumer(tools, args.consumer_batch_size, args.consumer_interval)
    workflow_executor.start()
    consumer.start()

    ApiHandler.orchestrator = orchestrator
    ApiHandler.store = store

    server = ThreadingHTTPServer((args.host, args.port), ApiHandler)
    print(f"Server listening on http://{args.host}:{args.port}", flush=True)
    print(f"Loaded capabilities: {', '.join(sorted({cap['name'] for cap in loaded_capabilities.values()}))}", flush=True)
    print(f"Loaded atomics: {', '.join(sorted({item['name'] for item in loaded_atomics}))}", flush=True)
    print("POST /api/messages with JSON: {\"conversation_id\":\"c1\",\"user_id\":\"u1\",\"text\":\"查日志 payment error\"}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down...", flush=True)
    finally:
        workflow_executor.stop_event.set()
        consumer.stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
