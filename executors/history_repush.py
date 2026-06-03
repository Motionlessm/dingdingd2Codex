import json
import os
import re
import sys
from datetime import datetime, timezone


STAGE_TABLES = {
    "pre_apasinfo": "pre_apasinfo",
    "pre_accept": "pre_accept",
    "pre_transact": "pre_transact",
}

TARGET_TABLE = "exception_to_atg_data"
CASE_ID_RE = re.compile(r"\b[A-Za-z0-9_-]{3,64}\b")


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))


def fail(message):
    emit({"status": "failed", "error": message, "message": message})
    return 0


def load_request():
    raw = sys.stdin.read()
    if not raw.strip():
        raise ValueError("empty executor input")
    return json.loads(raw)


def parse_case_ids(payload):
    values = []
    for key in ("case_ids", "proj_ids", "projIds", "ids", "items"):
        raw = payload.get(key)
        if isinstance(raw, list):
            values.extend(raw)
        elif isinstance(raw, str):
            values.extend(re.split(r"[\s,，、]+", raw))

    original = payload.get("_original_text") or payload.get("text") or ""
    if original:
        stop_words = {
            "history",
            "repush",
            "pre_apasinfo",
            "pre_accept",
            "pre_transact",
            TARGET_TABLE,
        }
        for token in CASE_ID_RE.findall(original):
            if token.lower() not in stop_words:
                values.append(token)

    seen = set()
    result = []
    for item in values:
        value = str(item).strip()
        if not CASE_ID_RE.fullmatch(value):
            continue
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def env(name, default=""):
    return os.environ.get(name, default).strip()


def mysql_config():
    host = env("ATG_REPUSH_MYSQL_HOST") or env("MYSQL_HOST")
    user = env("ATG_REPUSH_MYSQL_USER") or env("MYSQL_USER")
    password = os.environ.get("ATG_REPUSH_MYSQL_PASSWORD", os.environ.get("MYSQL_PASSWORD", ""))
    database = env("ATG_REPUSH_MYSQL_DATABASE") or env("MYSQL_DATABASE")
    port = int(env("ATG_REPUSH_MYSQL_PORT") or env("MYSQL_PORT") or "3306")
    charset = env("ATG_REPUSH_MYSQL_CHARSET") or env("MYSQL_CHARSET") or "utf8"
    missing = [
        name
        for name, value in {
            "ATG_REPUSH_MYSQL_HOST/MYSQL_HOST": host,
            "ATG_REPUSH_MYSQL_USER/MYSQL_USER": user,
            "ATG_REPUSH_MYSQL_DATABASE/MYSQL_DATABASE": database,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError("missing MySQL config: " + ", ".join(missing))
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "database": database,
        "charset": charset,
    }


def connect_mysql():
    config = mysql_config()
    try:
        import pymysql

        return pymysql.connect(
            host=config["host"],
            port=config["port"],
            user=config["user"],
            password=config["password"],
            database=config["database"],
            charset=config["charset"],
            autocommit=True,
        )
    except ImportError:
        try:
            import mysql.connector

            return mysql.connector.connect(
                host=config["host"],
                port=config["port"],
                user=config["user"],
                password=config["password"],
                database=config["database"],
                charset=config["charset"],
                autocommit=True,
            )
        except ImportError as exc:
            raise RuntimeError("install pymysql or mysql-connector-python for MySQL access") from exc


def placeholders(count):
    return ", ".join(["%s"] * count)


def insert_rows(conn, case_ids, table_name):
    now_text = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    rows = [(case_id, table_name, now_text) for case_id in case_ids]
    sql = f"""
        insert into {TARGET_TABLE}(projId, tableName, updateTime, result)
        values (%s, %s, %s, null)
        on duplicate key update
            updateTime = values(updateTime),
            result = null
    """
    with conn.cursor() as cur:
        cur.executemany(sql, rows)


def check_rows(conn, case_ids, table_name):
    sql = f"""
        select
            count(*) as total_count,
            sum(case when result is not null then 1 else 0 end) as done_count
        from {TARGET_TABLE}
        where tableName = %s
          and projId in ({placeholders(len(case_ids))})
    """
    with conn.cursor() as cur:
        cur.execute(sql, [table_name, *case_ids])
        row = cur.fetchone()
    total = int(row[0] or 0)
    done = int(row[1] or 0)
    missing = len(case_ids) - total
    pending = len(case_ids) - done
    return {
        "total": len(case_ids),
        "stored_count": total,
        "done_count": done,
        "missing_count": missing,
        "pending_count": pending,
    }


def main():
    request = load_request()
    stage = request.get("stage") or ""
    payload = request.get("payload") or {}
    stage_state = request.get("stage_state") or {}
    workflow_id = request.get("workflow_id") or ""
    executor = request.get("executor") or {}
    table_name = executor.get("business_table") or STAGE_TABLES.get(stage)

    if table_name not in STAGE_TABLES.values():
        return fail(f"unsupported repush stage: {stage}")

    case_ids = parse_case_ids(payload)
    if not case_ids:
        return fail("未找到合法单号，请在消息中提供单号，或传入 case_ids。")

    max_case_count = int(executor.get("max_case_count") or payload.get("max_case_count") or 5000)
    if len(case_ids) > max_case_count:
        return fail(f"单号数量 {len(case_ids)} 超过限制 {max_case_count}。")

    poll_seconds = int(executor.get("poll_interval_seconds") or payload.get("poll_interval_seconds") or 300)
    max_wait_seconds = int(executor.get("max_wait_seconds") or payload.get("max_wait_seconds") or 21600)

    try:
        conn = connect_mysql()
    except Exception as exc:
        return fail(str(exc))

    try:
        with conn:
            first_attempt = not stage_state
            if first_attempt:
                insert_rows(conn, case_ids, table_name)
                stats = check_rows(conn, case_ids, table_name)
                emit(
                    {
                        "status": "running" if stats["pending_count"] else "succeeded",
                        "job_id": f"{workflow_id}:{table_name}",
                        "submitted_count": len(case_ids),
                        "pending_count": stats["pending_count"],
                        "success_count": stats["done_count"],
                        "failed_count": stats["missing_count"],
                        "next_check_seconds": poll_seconds,
                        "max_wait_seconds": max_wait_seconds,
                        "message": (
                            f"历史数据重推：已写入阶段 {table_name}，单号 {len(case_ids)} 个；"
                            f"已完成 {stats['done_count']} 个，待处理 {stats['pending_count']} 个。"
                        ),
                    }
                )
                return 0

            stats = check_rows(conn, case_ids, table_name)
            if stats["missing_count"]:
                return fail(f"阶段 {table_name} 有 {stats['missing_count']} 个单号未查询到插入记录。")

            if stats["pending_count"]:
                emit(
                    {
                        "status": "running",
                        "job_id": stage_state.get("job_id") or f"{workflow_id}:{table_name}",
                        "submitted_count": len(case_ids),
                        "pending_count": stats["pending_count"],
                        "success_count": stats["done_count"],
                        "failed_count": 0,
                        "next_check_seconds": poll_seconds,
                        "max_wait_seconds": max_wait_seconds,
                        "message": (
                            f"历史数据重推：阶段 {table_name} 处理中，"
                            f"已完成 {stats['done_count']}/{len(case_ids)}，待处理 {stats['pending_count']}。"
                        ),
                    }
                )
                return 0

            emit(
                {
                    "status": "succeeded",
                    "job_id": stage_state.get("job_id") or f"{workflow_id}:{table_name}",
                    "submitted_count": len(case_ids),
                    "pending_count": 0,
                    "success_count": len(case_ids),
                    "failed_count": 0,
                    "message": f"历史数据重推：阶段 {table_name} 已全部完成，共 {len(case_ids)} 个单号。",
                }
            )
            return 0
    except Exception as exc:
        return fail(f"阶段 {table_name} 执行失败：{exc}")


if __name__ == "__main__":
    raise SystemExit(main())
