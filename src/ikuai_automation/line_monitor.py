from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .api import IkuaiClient
from .config import load_config

logger = logging.getLogger(__name__)
TZ = timezone(timedelta(hours=8))


def now_local() -> datetime:
    return datetime.now(TZ)


def line_key(device_name: str, name: str) -> str:
    return f"{device_name}::{name}"


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def make_client(device: dict[str, Any]) -> IkuaiClient:
    return IkuaiClient(
        f"{device['url']}:{device['port']}",
        str(device["username"]),
        str(device["password"]),
        device_name=str(device["device_name"]),
        verify_tls=bool(device.get("verify_tls", False)),
        timeout=(10, int(device.get("timeout_seconds", 60))),
    )


def classify_wan(item: dict[str, Any]) -> dict[str, Any]:
    username = str(item.get("username") or "").strip()
    comment = str(item.get("comment") or "").strip()
    name = str(item.get("name") or "").strip() or f"wan{item.get('id', '')}"
    line_name = username or comment or name

    if username:
        online = int(item.get("pppoe_status") or 0) == 1 and bool(item.get("pppoe_ip_addr"))
        return {"track": True, "mode": "PPPoE", "line_name": line_name, "online": online}

    dhcp_ip = str(item.get("dhcp_ip_addr") or "").strip()
    dhcp_status = int(item.get("dhcp_status") or 0)
    if dhcp_ip or dhcp_status == 1:
        return {"track": True, "mode": "DHCP", "line_name": comment or name, "online": dhcp_status == 1 and bool(dhcp_ip)}

    return {"track": False, "mode": "OTHER", "line_name": line_name, "online": None}


def _host_range(monitor_cfg: dict[str, Any]) -> range:
    start = int(monitor_cfg.get("lan_host_start", 11))
    end = int(monitor_cfg.get("lan_host_end", 43))
    return range(start, end + 1)


def build_expected_lan_map(
    monitor_cfg: dict[str, Any], device_name: str, dhcp_static_items: list[dict[str, Any]], previous_lines: dict[str, Any]
) -> tuple[dict[str, dict[str, str]], list[str]]:
    prefix = str(monitor_cfg.get("lan_prefix", "192.168.50."))
    host_range = _host_range(monitor_cfg)
    labels = monitor_cfg.get("manual_lan_labels") or {}
    expected: dict[str, dict[str, str]] = {}

    for item in dhcp_static_items:
        ip_addr = str(item.get("ip_addr") or "").strip()
        if not ip_addr.startswith(prefix):
            continue
        try:
            host = int(ip_addr.rsplit(".", 1)[1])
        except ValueError:
            continue
        if host not in host_range:
            continue
        expected[ip_addr] = {
            "comment": str(item.get("comment") or "").strip(),
            "hostname": str(item.get("hostname") or "").strip(),
            "mac": str(item.get("mac") or "").strip(),
        }

    missing: list[str] = []
    for host in host_range:
        ip_addr = f"{prefix}{host}"
        if ip_addr in expected:
            continue
        missing.append(ip_addr)
        prev = previous_lines.get(line_key(device_name, ip_addr), {})
        detail = prev.get("detail") if isinstance(prev.get("detail"), dict) else {}
        expected[ip_addr] = {
            "comment": str(prev.get("comment") or detail.get("comment") or labels.get(ip_addr) or ""),
            "hostname": str(prev.get("hostname") or detail.get("hostname") or ""),
            "mac": str(prev.get("mac") or detail.get("mac") or ""),
        }
    return expected, missing


def track_expected_lines(
    monitor_cfg: dict[str, Any],
    device_name: str,
    monitor_items: list[dict[str, Any]],
    previous_lines: dict[str, Any],
    new_lines: dict[str, Any],
    alerts: list[dict[str, Any]],
    recoveries: list[dict[str, Any]],
) -> tuple[int, int]:
    expected_lines = monitor_cfg.get("expected_lines") or {}
    prefix = str(monitor_cfg.get("line_prefix", "10.10.0."))
    miss_threshold = int(monitor_cfg.get("line_alert_after_misses", 2))
    online_by_username = {
        str(item.get("username") or "").strip(): item
        for item in monitor_items
        if str(item.get("ip_addr") or "").startswith(prefix)
    }

    if expected_lines and not online_by_username:
        for username, meta in expected_lines.items():
            key = line_key(device_name, f"line:{username}")
            prev = previous_lines.get(key, {})
            new_lines[key] = {
                "online": True,
                "mode": "LINE",
                "line_name": str(meta.get("display") or username),
                "detail": {"status": "unknown_no_line_entries_in_monitor_lanip"},
                "line_miss_count": 0,
                "previous_detail": prev.get("detail", {}),
            }
        return len(expected_lines), 0

    offline = 0
    for username, meta in expected_lines.items():
        display = str(meta.get("display") or username)
        key = line_key(device_name, f"line:{username}")
        prev = previous_lines.get(key, {})
        current = online_by_username.get(str(username))
        if current is not None:
            new_lines[key] = {"online": True, "mode": "LINE", "line_name": display, "detail": {"ip_addr": current.get("ip_addr", "")}, "line_miss_count": 0}
            if prev.get("online") is False and prev.get("notified_offline_at"):
                recoveries.append({"device": device_name, "line": display, "mode": "LINE"})
        else:
            offline += 1
            misses = int(prev.get("line_miss_count") or 0) + 1
            notified = prev.get("notified_offline_at")
            if misses >= miss_threshold and not notified:
                alerts.append({"device": device_name, "line": display, "mode": "LINE"})
                notified = now_local().isoformat()
            new_lines[key] = {"online": False, "mode": "LINE", "line_name": display, "line_miss_count": misses, "notified_offline_at": notified}
    return len(expected_lines), offline


def track_lan_devices(
    monitor_cfg: dict[str, Any],
    device_name: str,
    expected: dict[str, dict[str, str]],
    monitor_items: list[dict[str, Any]],
    previous_lines: dict[str, Any],
    new_lines: dict[str, Any],
    alerts: list[dict[str, Any]],
    recoveries: list[dict[str, Any]],
) -> tuple[int, int]:
    prefix = str(monitor_cfg.get("lan_prefix", "192.168.50."))
    labels = monitor_cfg.get("manual_lan_labels") or {}
    online_ips = {str(item.get("ip_addr")): item for item in monitor_items if str(item.get("ip_addr") or "").startswith(prefix)}
    offline = 0

    for ip_addr in sorted(expected, key=lambda value: [int(part) for part in value.split(".")]):
        meta = expected[ip_addr]
        key = line_key(device_name, ip_addr)
        prev = previous_lines.get(key, {})
        label = meta.get("comment") or meta.get("hostname") or labels.get(ip_addr) or ip_addr
        detail = {"ip_addr": ip_addr, **meta}
        if ip_addr in online_ips:
            new_lines[key] = {"online": True, "mode": "LAN", "line_name": label, "detail": detail, **meta}
            if prev.get("online") is False:
                recoveries.append({"device": device_name, "line": label, "mode": "LAN", "reason": detail})
        else:
            offline += 1
            notified = prev.get("notified_offline_at")
            if prev.get("online") is not False or not notified:
                alerts.append({"device": device_name, "line": label, "mode": "LAN", "reason": detail})
                notified = now_local().isoformat()
            new_lines[key] = {"online": False, "mode": "LAN", "line_name": label, "detail": detail, "notified_offline_at": notified, **meta}
    return len(expected), offline


def build_message(alerts: list[dict[str, Any]], recoveries: list[dict[str, Any]]) -> str:
    if not alerts and not recoveries:
        return ""
    parts = [now_local().strftime("%Y-%m-%d %H:%M %z")]
    for title, items in (("Detected offline/abnormal", alerts), ("Recovered", recoveries)):
        if not items:
            continue
        parts.extend(["", title + ":"])
        seen: set[str] = set()
        for item in items:
            label = f"[{item.get('mode', 'UNKNOWN')}] {item.get('line', '')}".strip()
            if label not in seen:
                parts.append(label)
                seen.add(label)
    return "\n".join(parts)


def run_monitor(config_path: str | Path, state_path: str | Path) -> dict[str, Any]:
    config = load_config(config_path)
    monitor_cfg = config.get("monitor") or {}
    enabled = set(monitor_cfg.get("enabled_devices") or [])
    state_path = Path(state_path)
    previous = load_state(state_path)
    previous_lines = previous.get("lines", {})
    new_state: dict[str, Any] = {"updated_at": now_local().isoformat(), "lines": {}}
    alerts: list[dict[str, Any]] = []
    recoveries: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []

    for device in config.get("data") or []:
        device_name = str(device.get("device_name") or device.get("url"))
        if enabled and device_name not in enabled:
            continue
        try:
            client = make_client(device)
            client.login()
            wan_items = client.wan()
            monitor_items = client.monitor_lanip()
            dhcp_static_items = client.dhcp_static()
            expected, missing = build_expected_lan_map(monitor_cfg, device_name, dhcp_static_items, previous_lines)
        except Exception as exc:
            logger.exception("Monitor collection failed for %s", device_name)
            summaries.append({"device": device_name, "status": "device_error", "detail": str(exc)})
            continue

        for item in wan_items:
            info = classify_wan(item)
            if not info["track"]:
                continue
            key = line_key(device_name, f"wan:{info['line_name']}")
            previous_online = previous_lines.get(key, {}).get("online")
            new_state["lines"][key] = info
            if info["online"] and previous_online is False:
                recoveries.append({"device": device_name, "line": info["line_name"], "mode": info["mode"]})
            elif info["online"] is False and previous_online is not False:
                alerts.append({"device": device_name, "line": info["line_name"], "mode": info["mode"]})

        line_tracked, line_offline = track_expected_lines(monitor_cfg, device_name, monitor_items, previous_lines, new_state["lines"], alerts, recoveries)
        lan_tracked, lan_offline = track_lan_devices(monitor_cfg, device_name, expected, monitor_items, previous_lines, new_state["lines"], alerts, recoveries)
        summaries.append({"device": device_name, "tracked_lines": line_tracked, "offline_lines": line_offline, "tracked_lan_devices": lan_tracked, "offline_lan_devices": lan_offline, "missing_lan_from_static_api": missing})

    save_state(state_path, new_state)
    return {"timestamp": now_local().isoformat(), "alerts": alerts, "recoveries": recoveries, "summaries": summaries, "message": build_message(alerts, recoveries)}


def main() -> None:
    parser = argparse.ArgumentParser(description="iKuai WAN/PPPoE/LAN state monitor")
    parser.add_argument("--config", default=os.environ.get("IKUAI_CONFIG", "config/config.json"))
    parser.add_argument("--state", default=os.environ.get("IKUAI_MONITOR_STATE", ".state/line-monitor.json"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(json.dumps(run_monitor(args.config, args.state), ensure_ascii=False))


if __name__ == "__main__":
    main()
