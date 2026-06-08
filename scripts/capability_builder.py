import argparse
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def slugify(text):
    text = re.sub(r"[^A-Za-z0-9]+", "-", text.lower()).strip("-")
    return text or "generated-capability"


def stage_key(text):
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(text).strip()).strip("_") or "stage"


def extract_code_block(text):
    match = re.search(r"```(?:sql)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else ""


def extract_create_table_name(text):
    match = re.search(r"create\s+table\s+`?([A-Za-z0-9_]+)`?", text, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    known = re.search(r"\b(exception_to_atg_data)\b", text, flags=re.IGNORECASE)
    if known:
        return known.group(1)
    table_hint = re.search(r"(?:table|表)\s*[`'\"]?([A-Za-z][A-Za-z0-9_]{2,})[`'\"]?", text, flags=re.IGNORECASE)
    return table_hint.group(1) if table_hint else ""


def extract_stage_values(text):
    values = re.findall(r"'([A-Za-z0-9_]+)'", text)
    values.extend(re.findall(r"\b(pre_[A-Za-z0-9_]+)\b", text))
    stages = []
    for value in values:
        if value.startswith("pre_") and value not in stages:
            stages.append(value)
    return stages


def infer_spec(description):
    table = extract_create_table_name(description)
    stages = extract_stage_values(description) or ["submit", "check"]
    name_hint = "history-data-repush" if table == "exception_to_atg_data" else slugify(description[:80])
    capability_name = slugify(name_hint)
    needs_mysql = bool(
        table
        or "mysql" in description.lower()
        or "table" in description.lower()
        or "数据库" in description
        or "写入" in description
        or "写" in description
        or "表" in description
    )
    needs_notify = "notify" in description.lower() or "钉钉" in description or "通知" in description

    atomics = []
    if needs_notify:
        atomics.append("dingtalk.notify")
    if needs_mysql:
        atomics.append(f"{capability_name}.submit")
        atomics.append(f"{capability_name}.check")

    return {
        "capability_name": capability_name,
        "label": capability_name.replace("-", " ").title(),
        "intent": capability_name.replace("-", "_"),
        "table": table,
        "stages": stages,
        "needs_mysql": needs_mysql,
        "needs_notify": needs_notify,
        "atomics": atomics,
        "source_sql": extract_code_block(description),
    }


def infer_task_types(spec, description):
    text = description.lower()
    types = []
    if spec["needs_mysql"]:
        types.append("database_write")
    if spec["needs_notify"]:
        types.append("notification")
    if len(spec["stages"]) > 1 or "async" in text or "异步" in description or "完成后" in description:
        types.append("long_running_workflow")
    if "log" in text or "日志" in description:
        types.append("log_lookup")
    if "http" in text or "api" in text or "接口" in description:
        types.append("http_api")
    return types or ["workflow"]


def infer_defaults(spec):
    defaults = []
    if spec["needs_mysql"]:
        env_name = "RETRY_PUSH_DB_DSN" if spec["table"] == "exception_to_atg_data" else f"{spec['intent'].upper()}_DB_DSN"
        defaults.append({"key": "dsn_env", "value": env_name, "reason": "database credentials must come from an environment variable"})
        defaults.append(
            {
                "key": "approval",
                "value": "submit atomics require requires_approval=true and risk=high",
                "reason": "database writes are risky operations",
            }
        )
        defaults.append(
            {
                "key": "completion_rule",
                "value": "empty or pending means pending; fail/error/失败 means failed; other non-empty result means success",
                "reason": "conservative default for asynchronous result fields",
            }
        )
    defaults.append({"key": "poll_interval_seconds", "value": 300, "reason": "default long-task polling interval"})
    defaults.append({"key": "max_wait_seconds", "value": 21600, "reason": "default long-task wait budget"})
    return defaults


def missing_questions(spec, description):
    questions = []
    lower = description.lower()
    if spec["needs_mysql"]:
        if "dsn" not in lower and "环境变量" not in description and "env" not in lower:
            default_env = "RETRY_PUSH_DB_DSN" if spec["table"] == "exception_to_atg_data" else f"{spec['intent'].upper()}_DB_DSN"
            questions.append(f"数据库连接环境变量名是否使用 {default_env}？")
        if "result" not in lower and "完成" not in description and "失败" not in description:
            questions.append("异步消费结果如何判断 pending/success/failed？")
        if not spec["table"]:
            questions.append("目标数据库表名是什么？")
    if not spec["stages"]:
        questions.append("业务阶段有哪些，按什么顺序执行？")
    if "通知" not in description and "notify" not in lower and "dingtalk" not in lower:
        questions.append("哪些节点需要发送钉钉通知？")
    return questions[:5]


def plan_summary(spec, description):
    stages = []
    for stage in spec["stages"]:
        if spec["needs_mysql"]:
            stages.append(f"submit_{stage} -> check_{stage}")
            if spec["needs_notify"]:
                stages.append(f"notify_{stage}")
        else:
            stages.append(stage)
    return {
        "task_types": infer_task_types(spec, description),
        "inferred": {
            "capability_name": spec["capability_name"],
            "table": spec["table"],
            "business_stages": spec["stages"],
            "atomics": spec["atomics"],
        },
        "questions": missing_questions(spec, description),
        "defaults": infer_defaults(spec),
        "workflow_plan": stages,
        "implementation_gate": "Confirm the plan before using --apply or editing project files.",
    }


def capability_json(spec):
    stages = []
    if spec["needs_notify"]:
        stages.append(
            {
                "name": "notify_start",
                "label": "Notify start",
                "executor": {
                    "type": "atomic",
                    "name": "dingtalk.notify",
                    "input": {"message": "Workflow {workflow_id} started: {skill}"},
                },
            }
        )
    for stage in spec["stages"]:
        key = stage_key(stage)
        if spec["needs_mysql"]:
            stages.append(
                {
                    "name": f"submit_{key}",
                    "label": f"Submit {stage}",
                    "executor": {
                        "type": "atomic",
                        "name": f"{spec['capability_name']}.submit",
                        "input": {
                            "operation": "submit",
                            "business_stage": stage,
                            "table": spec["table"],
                            "items": "$payload.items",
                            "case_ids": "$payload.case_ids",
                            "batch_size": "$payload.batch_size",
                            "poll_interval_seconds": "$payload.poll_interval_seconds",
                            "max_wait_seconds": "$payload.max_wait_seconds",
                        },
                    },
                }
            )
            stages.append(
                {
                    "name": f"check_{key}",
                    "label": f"Check {stage}",
                    "executor": {
                        "type": "atomic",
                        "name": f"{spec['capability_name']}.check",
                        "input": {
                            "operation": "check",
                            "business_stage": stage,
                            "table": spec["table"],
                            "items": "$payload.items",
                            "case_ids": "$payload.case_ids",
                            "poll_interval_seconds": "$payload.poll_interval_seconds",
                            "max_wait_seconds": "$payload.max_wait_seconds",
                        },
                    },
                    "poll_interval_seconds": 300,
                    "max_wait_seconds": 21600,
                }
            )
        else:
            stages.append(
                {
                    "name": key,
                    "label": f"Run {stage}",
                    "executor": {
                        "type": "command",
                        "command": ["python", f"executors/{spec['capability_name'].replace('-', '_')}.py"],
                        "stage_name": stage,
                        "timeout_seconds": 30,
                        "poll_interval_seconds": 300,
                        "max_wait_seconds": 21600,
                    },
                }
            )
        if spec["needs_notify"]:
            stages.append(
                {
                    "name": f"notify_{key}",
                    "label": f"Notify {stage}",
                    "executor": {
                        "type": "atomic",
                        "name": "dingtalk.notify",
                        "input": {"message": f"Workflow {{workflow_id}} completed business stage {stage}"},
                    },
                }
            )
    if not spec["needs_mysql"] and not spec["stages"]:
        stages.append(
            {
                "name": "run",
                "label": "Run",
                "executor": {
                    "type": "command",
                    "command": ["python", f"executors/{spec['capability_name'].replace('-', '_')}.py"],
                    "timeout_seconds": 30,
                    "poll_interval_seconds": 300,
                    "max_wait_seconds": 21600,
                },
            }
        )
    return {
        "name": spec["capability_name"],
        "label": spec["label"],
        "intent": spec["intent"],
        "aliases": [spec["intent"]],
        "triggers": [spec["capability_name"], spec["label"].lower()],
        "created_message": f"Created {spec['label']} workflow",
        "input_defaults": {"items": [], "case_ids": [], "batch_size": 2000, "poll_interval_seconds": 300, "max_wait_seconds": 21600},
        "stages": stages,
    }


def executor_stub(spec):
    table = spec["table"] or "TODO_TABLE"
    stages = spec["stages"]
    return f'''import json
import sys


ALLOWED_STAGES = {stages!r}
TARGET_TABLE = {table!r}


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))


def fail(message):
    emit({{"status": "failed", "error": message, "message": message}})
    return 0


def main():
    request = json.loads(sys.stdin.read() or "{{}}")
    stage = request.get("stage") or ""
    payload = request.get("payload") or {{}}
    atomic_input = request.get("atomic_input") or {{}}
    business_stage = atomic_input.get("business_stage") or stage
    operation = atomic_input.get("operation") or "run"
    if business_stage not in ALLOWED_STAGES:
        return fail(f"unsupported business stage: {{business_stage}}")
    if operation not in {{"submit", "check", "run"}}:
        return fail(f"unsupported operation: {{operation}}")

    # TODO: replace this stub with a controlled submit/check implementation.
    # Keep each invocation short. Return running with next_check_seconds when
    # downstream processing is not done yet; do not sleep in this script.
    emit({{
        "status": "failed",
        "error": "executor stub is not implemented",
        "message": f"Executor for {{stage}} is not implemented. target_table={{TARGET_TABLE}} payload_keys={{sorted(payload.keys())}}",
    }})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def atomic_submit_json(spec):
    safe = spec["capability_name"]
    return {
        "name": f"{safe}.submit",
        "label": f"{spec['label']} submit",
        "description": f"Submit records for {spec['label']} through a controlled command executor.",
        "type": "command",
        "risk": "high",
        "requires_approval": True,
        "executor": {
            "command": ["python", f"executors/{safe.replace('-', '_')}.py"],
            "timeout_seconds": 30,
        },
        "table": spec["table"],
    }


def atomic_check_json(spec):
    safe = spec["capability_name"]
    return {
        "name": f"{safe}.check",
        "label": f"{spec['label']} check",
        "description": f"Check processing progress for {spec['label']} through a controlled command executor.",
        "type": "command",
        "risk": "low",
        "requires_approval": False,
        "executor": {
            "command": ["python", f"executors/{safe.replace('-', '_')}.py"],
            "timeout_seconds": 30,
        },
        "table": spec["table"],
    }


def build_files(spec):
    safe = spec["capability_name"]
    files = {
        f"capabilities/{safe}.json": json.dumps(capability_json(spec), ensure_ascii=False, indent=2) + "\n",
        f"executors/{safe.replace('-', '_')}.py": executor_stub(spec),
    }
    if spec["needs_mysql"]:
        files[f"atomics/{safe}-submit.json"] = json.dumps(atomic_submit_json(spec), ensure_ascii=False, indent=2) + "\n"
        files[f"atomics/{safe}-check.json"] = json.dumps(atomic_check_json(spec), ensure_ascii=False, indent=2) + "\n"
    return files


def main():
    parser = argparse.ArgumentParser(description="Generate capability/atomic/executor drafts from a natural-language description.")
    parser.add_argument("--description", "-d", default="", help="Natural-language task description.")
    parser.add_argument("--file", "-f", help="Read task description from a text file.")
    parser.add_argument("--apply", action="store_true", help="Write generated files into the project.")
    args = parser.parse_args()

    description = args.description
    if args.file:
        description = Path(args.file).read_text(encoding="utf-8")
    if not description.strip():
        description = sys.stdin.read()
    if not description.strip():
        raise SystemExit("description is required")

    spec = infer_spec(description)
    files = build_files(spec)
    output = {"spec": spec, "plan": plan_summary(spec, description), "files": sorted(files)}
    print(json.dumps(output, ensure_ascii=False, indent=2))

    if args.apply:
        for rel_path, content in files.items():
            path = ROOT / rel_path
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                raise SystemExit(f"refusing to overwrite existing file: {rel_path}")
            path.write_text(content, encoding="utf-8")
        print("generated files written", file=sys.stderr)


if __name__ == "__main__":
    main()
