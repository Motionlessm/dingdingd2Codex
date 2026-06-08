import json
import os
import re
import sys
from urllib.parse import parse_qs, urlparse


WRITE_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|replace|merge|grant|revoke|call|exec|load|outfile)\b",
    re.IGNORECASE,
)
TABLE_PATTERN = re.compile(r"\b(?:from|join)\s+`?([A-Za-z0-9_.$]+)`?", re.IGNORECASE)


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


def normalize_sql(sql):
    return " ".join(str(sql or "").strip().split())


def validate_sql(sql, allowed_tables):
    normalized = normalize_sql(sql)
    if not normalized:
        raise ValueError("sql is required")
    lowered = normalized.lower()
    if not (lowered.startswith("select ") or lowered.startswith("with ")):
        raise ValueError("only SELECT/WITH read queries are allowed")
    if WRITE_KEYWORDS.search(normalized):
        raise ValueError("write or DDL keyword is not allowed")
    tables = {m.group(1).split(".")[-1] for m in TABLE_PATTERN.finditer(normalized)}
    if allowed_tables:
        denied = sorted(t for t in tables if t not in allowed_tables)
        if denied:
            raise ValueError(f"query touches non-allowlisted table(s): {', '.join(denied)}")
    return normalized, sorted(tables)


def parse_dsn(dsn):
    parsed = urlparse(dsn)
    if parsed.scheme not in {"mysql", "mysql+pymysql"}:
        raise ValueError("dsn must use mysql://")
    query = parse_qs(parsed.query)
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 3306,
        "user": parsed.username or "",
        "password": parsed.password or "",
        "database": parsed.path.lstrip("/"),
        "charset": (query.get("charset") or ["utf8mb4"])[0],
    }


def connect_mysql(dsn):
    config = parse_dsn(dsn)
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
            cursorclass=pymysql.cursors.DictCursor,
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


def main():
    request = load_request()
    atomic = request.get("atomic") or {}
    atomic_input = request.get("atomic_input") or {}
    dsn_env = atomic.get("dsn_env") or "ATOMIC_MYSQL_DSN"
    dsn = os.environ.get(dsn_env, "").strip()
    if not dsn:
        return fail(f"missing MySQL DSN environment variable: {dsn_env}")

    allowed_tables = set(atomic.get("allowed_tables") or [])
    max_rows = int(atomic_input.get("max_rows") or atomic.get("max_rows") or 100)
    max_rows = max(1, min(max_rows, 1000))
    sql, tables = validate_sql(atomic_input.get("sql"), allowed_tables)
    params = atomic_input.get("params") or []
    if not isinstance(params, list):
        return fail("params must be a list")

    if " limit " not in sql.lower():
        sql = f"{sql} limit {max_rows}"

    try:
        conn = connect_mysql(dsn)
        with conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            rows = cur.fetchall()
            if rows and not isinstance(rows[0], dict):
                columns = [item[0] for item in cur.description or []]
                rows = [dict(zip(columns, row)) for row in rows]
    except Exception as exc:
        return fail(str(exc))

    emit(
        {
            "status": "succeeded",
            "submitted_count": 1,
            "pending_count": 0,
            "success_count": 1,
            "failed_count": 0,
            "tables": tables,
            "row_count": len(rows),
            "rows": rows[:max_rows],
            "message": f"MySQL read query succeeded, rows={len(rows)}",
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
