import json
import os
import sys
from urllib import request as urlrequest
from urllib.parse import urlencode, urlparse


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


def allowed_hosts(config):
    hosts = set(config.get("allowed_hosts") or [])
    env_hosts = os.environ.get("ATOMIC_HTTP_ALLOWED_HOSTS", "")
    for item in env_hosts.split(","):
        item = item.strip()
        if item:
            hosts.add(item)
    return hosts


def main():
    request = load_request()
    atomic = request.get("atomic") or {}
    atomic_input = request.get("atomic_input") or {}
    method = str(atomic_input.get("method") or "GET").upper()
    allowed_methods = {str(x).upper() for x in atomic.get("methods") or ["GET"]}
    if method not in allowed_methods:
        return fail(f"method not allowed: {method}")

    url = str(atomic_input.get("url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return fail("url must be an absolute http(s) URL")
    hosts = allowed_hosts(atomic)
    if hosts and parsed.hostname not in hosts:
        return fail(f"host not allowlisted: {parsed.hostname}")
    if not hosts:
        return fail("no HTTP hosts are allowlisted")

    headers = atomic_input.get("headers") or {}
    if not isinstance(headers, dict):
        return fail("headers must be an object")
    body = atomic_input.get("body")
    data = None
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        else:
            data = str(body).encode("utf-8")
    elif method == "POST":
        form = atomic_input.get("form")
        if form is not None:
            data = urlencode(form).encode("utf-8")
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    timeout = int(atomic_input.get("timeout_seconds") or atomic.get("timeout_seconds") or 15)
    max_bytes = int(atomic.get("max_response_bytes") or 20000)
    req = urlrequest.Request(url, data=data, headers=headers, method=method)
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(max_bytes + 1)
            truncated = len(raw) > max_bytes
            raw = raw[:max_bytes]
            text = raw.decode(resp.headers.get_content_charset() or "utf-8", errors="replace")
            status_code = resp.status
            response_headers = dict(resp.headers.items())
    except Exception as exc:
        return fail(str(exc))

    emit(
        {
            "status": "succeeded" if status_code < 400 else "failed",
            "submitted_count": 1,
            "pending_count": 0,
            "success_count": 1 if status_code < 400 else 0,
            "failed_count": 0 if status_code < 400 else 1,
            "status_code": status_code,
            "headers": response_headers,
            "body": text,
            "truncated": truncated,
            "message": f"HTTP {method} {parsed.hostname} returned {status_code}",
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
