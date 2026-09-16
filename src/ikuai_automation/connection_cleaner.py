from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from .api import IkuaiClient
from .config import load_config

logger = logging.getLogger(__name__)

DEFAULT_LOW_UPLOAD_THRESHOLD = 1_000_000
DEFAULT_LOW_UPLOAD_SAMPLES = 2
DEFAULT_COOLDOWN_SECONDS = 3600
DEFAULT_POLLING_SECONDS = 900
DEFAULT_LIVE_UPLOAD_THRESHOLD = 300_000
DEFAULT_LIVE_MIN_CONNECT_NUM = 80
DEFAULT_ACTIVE_GROUP_WINDOWS = [
    {"start": "00:00", "end": "09:00", "groups": ["1"], "allow_clear": True},
    {"start": "09:00", "end": "13:30", "groups": ["3"], "allow_clear": True},
    {"start": "13:30", "end": "18:00", "groups": ["2"], "allow_clear": True},
    {"start": "18:00", "end": "24:00", "groups": [], "allow_clear": False},
]
DEFAULT_GROUP_RANGES = {
    "1": [[11, 21]],
    "2": [[22, 33]],
    "3": [[34, 50]],
}


def parse_hhmm(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def current_window(config: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now()
    minute = now.hour * 60 + now.minute
    windows = config.get("active_group_windows") or DEFAULT_ACTIVE_GROUP_WINDOWS
    for window in windows:
        if parse_hhmm(window["start"]) <= minute < parse_hhmm(window["end"]):
            return window
    return {"groups": [], "allow_clear": False, "start": "unknown", "end": "unknown"}


def ip_group_from_row(row: dict[str, Any], device: dict[str, Any]) -> str | None:
    comment = str(row.get("comment") or "")
    configured_groups = device.get("group_ranges") or DEFAULT_GROUP_RANGES
    for group in configured_groups:
        if comment.startswith(str(group)):
            return str(group)

    ip_addr = str(row.get("ip_addr") or "")
    try:
        last_octet = int(ip_addr.rsplit(".", 1)[1])
    except Exception:
        return None

    for group, ranges in configured_groups.items():
        for start, end in ranges:
            if int(start) <= last_octet <= int(end):
                return str(group)
    return None


def ip_in_range(ip_addr: str, range_spec: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_addr)
        if "-" in range_spec:
            start, end = range_spec.split("-", 1)
            return ipaddress.ip_address(start.strip()) <= ip <= ipaddress.ip_address(end.strip())
        if "/" in range_spec:
            return ip in ipaddress.ip_network(range_spec.strip(), strict=False)
        return ip == ipaddress.ip_address(range_spec.strip())
    except ValueError:
        return False


def ip_allowed_for_clear(ip_addr: str, device: dict[str, Any]) -> bool:
    return any(ip_in_range(ip_addr, spec) for spec in device.get("allowed_clear_ranges", []))


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"ips": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(state, dict):
            state.setdefault("ips", {})
            return state
    except Exception as exc:
        logger.warning("Could not read state file %s: %s", path, exc)
    return {"ips": {}}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def state_key(device_name: str, ip_addr: str) -> str:
    return f"{device_name}|{ip_addr}"


def reset_low_count(device_name: str, ip_addr: str, state: dict[str, Any], reason: str, now: datetime) -> None:
    item = state.setdefault("ips", {}).setdefault(
        state_key(device_name, ip_addr), {"ip": ip_addr, "device_name": device_name}
    )
    item["low_count"] = 0
    item["last_low_reason"] = reason
    item["last_seen_ts"] = now.timestamp()


def should_track_and_clear(
    row: dict[str, Any], device: dict[str, Any], state: dict[str, Any], now: datetime
) -> tuple[bool, str, dict[str, Any]]:
    ip_addr = str(row.get("ip_addr") or "")
    comment = str(row.get("comment") or "")
    upload = int(row.get("upload") or 0)
    connect_num = int(row.get("connect_num") or 0)
    now_ts = now.timestamp()

    if ip_addr in set(device.get("except_ips") or []):
        return False, "except_ip", {"upload": upload, "connect_num": connect_num}

    if not ip_allowed_for_clear(ip_addr, device):
        reset_low_count(device["device_name"], ip_addr, state, "outside_allowed_clear_ranges", now)
        return False, "outside_allowed_clear_ranges", {"upload": upload, "connect_num": connect_num}

    window = current_window(device, now)
    active_groups = {str(group) for group in window.get("groups") or []}
    group = ip_group_from_row(row, device)

    if not bool(window.get("allow_clear", False)):
        reset_low_count(device["device_name"], ip_addr, state, "clear_disabled_window", now)
        return False, "clear_disabled_window", {"upload": upload, "connect_num": connect_num, "group": group}

    if group not in active_groups:
        reset_low_count(device["device_name"], ip_addr, state, "inactive_group", now)
        return False, "inactive_group", {"upload": upload, "connect_num": connect_num, "group": group}

    is_live = ip_addr in set(device.get("live_special_ips") or [])
    if is_live:
        threshold = int(device.get("live_upload_value", DEFAULT_LIVE_UPLOAD_THRESHOLD))
        min_connections = int(device.get("live_min_connect_num", DEFAULT_LIVE_MIN_CONNECT_NUM))
        low = upload < threshold and connect_num >= min_connections
        reason = f"live_low_upload<{threshold}_conn>={min_connections}"
    else:
        threshold = int(device.get("upload_value", DEFAULT_LOW_UPLOAD_THRESHOLD))
        low = upload < threshold
        reason = f"low_upload<{threshold}"

    item = state.setdefault("ips", {}).setdefault(state_key(device["device_name"], ip_addr), {})
    item.update(
        {
            "ip": ip_addr,
            "comment": comment,
            "device_name": device["device_name"],
            "group": group,
            "is_live_special": is_live,
            "last_seen_ts": now_ts,
            "last_upload": upload,
            "last_connect_num": connect_num,
        }
    )

    if not low:
        item["low_count"] = 0
        item["last_low_reason"] = "not_low"
        return False, "not_low", {"upload": upload, "connect_num": connect_num, "group": group, "is_live": is_live}

    low_count = int(item.get("low_count") or 0) + 1
    item["low_count"] = low_count
    item["last_low_ts"] = now_ts
    item["last_low_reason"] = reason

    required = int(device.get("low_upload_samples", DEFAULT_LOW_UPLOAD_SAMPLES))
    if low_count < required:
        return False, f"low_sample_{low_count}/{required}", {"upload": upload, "connect_num": connect_num, "group": group, "is_live": is_live}

    cooldown = int(device.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS))
    last_clear = item.get("last_clear_ts")
    if last_clear:
        elapsed = max(0.0, now_ts - float(last_clear))
        if elapsed < cooldown:
            return False, f"cooldown_remaining_{int(cooldown - elapsed)}s", {"upload": upload, "connect_num": connect_num, "group": group, "is_live": is_live}

    return True, reason, {"upload": upload, "connect_num": connect_num, "group": group, "is_live": is_live, "low_count": low_count}


def mark_cleared(device_name: str, ip_addr: str, state: dict[str, Any], now: datetime) -> None:
    item = state.setdefault("ips", {}).setdefault(
        state_key(device_name, ip_addr), {"ip": ip_addr, "device_name": device_name}
    )
    item["last_clear_ts"] = now.timestamp()
    item["last_clear_at"] = now.isoformat(timespec="seconds")
    item["low_count"] = 0
    item["clear_count"] = int(item.get("clear_count") or 0) + 1


def make_client(device: dict[str, Any]) -> IkuaiClient:
    return IkuaiClient(
        f"{device['url']}:{device['port']}",
        str(device["username"]),
        str(device["password"]),
        device_name=str(device["device_name"]),
        verify_tls=bool(device.get("verify_tls", False)),
    )


def run_cycle(device: dict[str, Any], state_path: Path, *, dry_run: bool = False) -> int:
    client = make_client(device)
    client.login()
    rows = client.monitor_lanip(limit=300)
    now = datetime.now()
    state = load_state(state_path)
    cleared = 0
    limit = int(device.get("max_clears_per_cycle", 8))

    for row in rows:
        ip_addr = str(row.get("ip_addr") or "")
        if not ip_addr:
            continue
        should_clear, reason, meta = should_track_and_clear(row, device, state, now)
        if not should_clear or cleared >= limit:
            continue
        if dry_run:
            logger.warning("DRY-RUN: would clear %s (%s): %s %s", row.get("comment", ""), ip_addr, reason, meta)
            continue
        if client.clear_connections(ip_addr):
            mark_cleared(str(device["device_name"]), ip_addr, state, now)
            cleared += 1
            logger.warning("Cleared connections for %s (%s): %s", row.get("comment", ""), ip_addr, reason)

    state["last_cycle_at"] = now.isoformat(timespec="seconds")
    state["last_cycle_device"] = device["device_name"]
    if not dry_run:
        save_state(state_path, state)
    return cleared


def process_device(device: dict[str, Any], state_dir: Path, *, once: bool, dry_run: bool) -> None:
    state_path = state_dir / f"connection-cleaner-{device['device_name']}.json"
    while True:
        try:
            run_cycle(device, state_path, dry_run=dry_run)
        except Exception:
            logger.exception("Connection-cleaner cycle failed for %s", device.get("device_name"))
        if once:
            return
        time.sleep(int(device.get("polling", DEFAULT_POLLING_SECONDS)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Stateful low-upload connection cleaner for iKuai")
    parser.add_argument("--config", default=os.environ.get("IKUAI_CONFIG", "config/config.json"))
    parser.add_argument("--state-dir", default=os.environ.get("IKUAI_STATE_DIR", ".state"))
    parser.add_argument("--once", action="store_true", default=os.environ.get("IKUAI_CONN_CLEANER_ONCE", "").lower() in {"1", "true", "yes"})
    parser.add_argument("--dry-run", action="store_true", default=os.environ.get("IKUAI_CONN_CLEANER_DRY_RUN", "").lower() in {"1", "true", "yes"})
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config(args.config)
    devices = config.get("data") or []
    if not devices:
        raise SystemExit("No devices configured")

    state_dir = Path(args.state_dir)
    with ThreadPoolExecutor(max_workers=len(devices)) as executor:
        futures = [executor.submit(process_device, device, state_dir, once=args.once, dry_run=args.dry_run) for device in devices]
        for future in as_completed(futures):
            future.result()


if __name__ == "__main__":
    main()
