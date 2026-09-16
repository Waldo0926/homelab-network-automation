from datetime import datetime

from ikuai_automation.connection_cleaner import ip_in_range, should_track_and_clear


def device_config():
    return {
        "device_name": "test-router",
        "allowed_clear_ranges": ["192.168.50.11-192.168.50.50"],
        "except_ips": [],
        "live_special_ips": [],
        "upload_value": 1_000_000,
        "low_upload_samples": 2,
        "cooldown_seconds": 3600,
        "group_ranges": {"1": [[11, 21]]},
        "active_group_windows": [{"start": "00:00", "end": "24:00", "groups": ["1"], "allow_clear": True}],
    }


def test_ip_range_supports_range_and_cidr():
    assert ip_in_range("192.168.50.20", "192.168.50.11-192.168.50.50")
    assert ip_in_range("192.168.50.20", "192.168.50.0/24")
    assert not ip_in_range("192.168.51.20", "192.168.50.0/24")


def test_requires_consecutive_low_upload_samples():
    device = device_config()
    state = {"ips": {}}
    row = {"ip_addr": "192.168.50.12", "comment": "1-node", "upload": 100, "connect_num": 20}
    now = datetime(2026, 1, 1, 1, 0)

    first, reason, _ = should_track_and_clear(row, device, state, now)
    assert first is False
    assert reason == "low_sample_1/2"

    second, reason, _ = should_track_and_clear(row, device, state, now)
    assert second is True
    assert reason.startswith("low_upload<")


def test_except_ip_is_never_cleared():
    device = device_config()
    device["except_ips"] = ["192.168.50.12"]
    state = {"ips": {}}
    row = {"ip_addr": "192.168.50.12", "comment": "1-node", "upload": 0, "connect_num": 100}
    should_clear, reason, _ = should_track_and_clear(row, device, state, datetime(2026, 1, 1, 1, 0))
    assert should_clear is False
    assert reason == "except_ip"
