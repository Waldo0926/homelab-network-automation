from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import request

from .line_monitor import run_monitor

TZ = timezone(timedelta(hours=8))


def now_local_iso() -> str:
    return datetime.now(TZ).isoformat()


def api_host() -> str:
    domain = os.environ.get("FEISHU_DOMAIN", "feishu").strip().lower()
    return "open.feishu.cn" if domain == "feishu" else "open.larksuite.com"


def post_json(url: str, payload: dict, headers: dict[str, str] | None = None, timeout: int = 20) -> dict:
    req = request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def tenant_access_token() -> str:
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise RuntimeError("FEISHU_APP_ID and FEISHU_APP_SECRET are required")
    data = post_json(
        f"https://{api_host()}/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": app_id, "app_secret": app_secret},
    )
    token = data.get("tenant_access_token")
    if data.get("code") != 0 or not token:
        raise RuntimeError(f"Could not get tenant access token: code={data.get('code')}")
    return str(token)


def send_text(text: str) -> dict:
    chat_id = os.environ.get("FEISHU_CHAT_ID", "").strip()
    if not chat_id:
        raise RuntimeError("FEISHU_CHAT_ID is required")
    payload = {"receive_id": chat_id, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)}
    data = post_json(
        f"https://{api_host()}/open-apis/im/v1/messages?receive_id_type=chat_id",
        payload,
        headers={"Authorization": f"Bearer {tenant_access_token()}"},
    )
    if data.get("code") != 0:
        raise RuntimeError(f"Feishu send failed: code={data.get('code')}")
    return data


def load_pending(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except Exception:
        return []


def save_pending(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def enqueue(items: list[dict], text: str, source: str) -> list[dict]:
    text = text.strip()
    if not text or any(item.get("text") == text for item in items):
        return items
    items.append({"text": text, "source": source, "created_at": now_local_iso(), "attempts": 0})
    return items


def flush(items: list[dict]) -> tuple[list[dict], list[dict]]:
    sent: list[dict] = []
    remaining: list[dict] = []
    for index, item in enumerate(items):
        item["attempts"] = int(item.get("attempts") or 0) + 1
        item["last_attempt_at"] = now_local_iso()
        try:
            response = send_text(str(item.get("text") or ""))
            item["message_id"] = ((response.get("data") or {}).get("message_id") or "")
            sent.append(item)
        except Exception as exc:
            item["last_error"] = str(exc)
            remaining.extend(items[index:])
            break
    return sent, remaining


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the iKuai monitor and send alerts to Feishu/Lark")
    parser.add_argument("--config", default=os.environ.get("IKUAI_CONFIG", "config/config.json"))
    parser.add_argument("--state", default=os.environ.get("IKUAI_MONITOR_STATE", ".state/line-monitor.json"))
    parser.add_argument("--queue", default=os.environ.get("IKUAI_NOTIFY_QUEUE", ".state/notification-queue.json"))
    parser.add_argument("--send-text", help="Send a manual test message instead of running the monitor")
    args = parser.parse_args()

    queue_path = Path(args.queue)
    pending = load_pending(queue_path)
    result = None
    if args.send_text:
        pending = enqueue(pending, args.send_text, "manual")
    else:
        result = run_monitor(args.config, args.state)
        pending = enqueue(pending, str(result.get("message") or ""), "monitor")

    sent, remaining = flush(pending)
    save_pending(queue_path, remaining)
    print(json.dumps({"timestamp": now_local_iso(), "monitor_result": result, "sent_count": len(sent), "remaining_count": len(remaining)}, ensure_ascii=False))
    if remaining:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
