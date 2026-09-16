from ikuai_automation.line_monitor import build_message, classify_wan


def test_pppoe_wan_online_detection():
    result = classify_wan({"username": "line-1", "pppoe_status": 1, "pppoe_ip_addr": "198.51.100.10"})
    assert result["track"] is True
    assert result["mode"] == "PPPoE"
    assert result["online"] is True


def test_alert_message_contains_alert_and_recovery():
    message = build_message(
        [{"mode": "LAN", "line": "node-a"}],
        [{"mode": "LINE", "line": "WAN-101"}],
    )
    assert "node-a" in message
    assert "WAN-101" in message
    assert "Detected offline/abnormal" in message
    assert "Recovered" in message
