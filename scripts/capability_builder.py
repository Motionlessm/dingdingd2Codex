import argparse
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def slugify(text):
    text = re.sub(r"[^A-Za-z0-9]+", "-", text.lower()).strip("-")
    return text or "generated-capability"


def extract_code_block(text):
    match = re.search(r"```(?:sql)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else ""


def extract_create_table_name(text):
    match = re.search(r"create\s+table\s+`?([A-Za-z0-9_]+)`?", text, flags=re.IGNORECASE)
    return match.group(1) if match else ""


def extract_stage_values(text):
    values = re.findall(r"'([A-Za-z0-9_]+)'", text)
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
    needs_mysql = bool(table or "mysql" in description.lower() or "table" in description.lower())
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
        stages.append(
            {
                "name": stage,
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
                    "name": f"notify_{stage}",
                    "label": f"Notify {stage}",
                    "executor": {
                        "type": "atomic",
                        "name": "dingtalk.notify",
                        "input": {"message": f"Workflow {{workflow_id}} completed stage {stage}"},
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
        "input_defaults": {"items": [], "poll_interval_seconds": 300, "max_wait_seconds": 21600},
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
    if stage not in ALLOWED_STAGES:
        return fail(f"unsupported stage: {{stage}}")

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
    output = {"spec": spec, "files": sorted(files)}
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
