# Homelab Network Automation

> **Status: Archived / Portfolio Project**  
> A sanitized reconstruction of automation that previously ran against an iKuai gateway in a personal homelab. The original deployment has been retired.

Python tooling for **iKuai router API automation**, including stateful low-upload connection cleanup, WAN/PPPoE/LAN health monitoring, alert deduplication/recovery tracking, and optional Feishu/Lark notifications.

## Why this project exists

The original environment had multiple downstream hosts and WAN/PPPoE lines. Manual inspection was repetitive, while blindly resetting connections would have been unsafe. The automation therefore used explicit address allow-lists, active-group schedules, consecutive low-throughput samples, per-host cooldowns, per-cycle limits and persistent state before performing a connection reset.

The monitoring side combined multiple iKuai API views and retained state between runs so transient API omissions would not immediately become outage alerts.

## Highlights

- Small reusable client for iKuai `/Action/login` and `/Action/call`
- Stateful connection-cleaning policy with **allow-list + exception list + cooldown + consecutive-sample guards**
- Configurable time windows and endpoint groups
- Separate handling for selected high-connection-count hosts
- WAN/PPPoE and LAN presence monitoring
- Consecutive-miss suppression for transient line-monitoring gaps
- Recovery notifications and persistent alert state
- Feishu/Lark notification queue with retry persistence
- systemd and cron/flock deployment examples
- Dry-run mode before destructive connection-reset operations

## Repository layout

```text
src/ikuai_automation/
  api.py                  iKuai HTTP API client
  config.py               JSON config + ${ENV_VAR} expansion
  connection_cleaner.py   stateful connection-reset policy
  line_monitor.py         WAN/PPPoE/LAN monitoring
  feishu_notifier.py      notification + retry queue
config/
  config.example.json
deployment/
  systemd/
  cron/
docs/
  architecture.md
tests/
```

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp config/config.example.json config/config.json
cp .env.example .env
```

Load environment variables using your preferred secret-management method. For a local shell:

```bash
set -a
source .env
set +a
```

Run tests:

```bash
pytest -q
```

Run a **non-destructive** cleaner pass first:

```bash
ikuai-cleaner --config config/config.json --once --dry-run
```

Run one monitor pass:

```bash
ikuai-monitor --config config/config.json
```

Run the monitor and send a notification when a new alert/recovery exists:

```bash
ikuai-notify --config config/config.json
```

## Configuration

`config/config.example.json` contains documentation-only addresses and labels. The `username` and `password` fields demonstrate `${ENV_VAR}` expansion so secrets do not need to be stored in Git.

Important cleaner controls include:

| Key | Purpose |
| --- | --- |
| `allowed_clear_ranges` | Hard boundary around addresses eligible for reset |
| `except_ips` | Explicit addresses that must never be reset |
| `low_upload_samples` | Consecutive low-throughput observations required |
| `cooldown_seconds` | Minimum time before the same address can be reset again |
| `max_clears_per_cycle` | Limits blast radius during a single poll |
| `active_group_windows` | Time-based group activation and clear enable/disable |
| `live_special_ips` | Hosts using the alternate upload + connection-count rule |

See [`docs/architecture.md`](docs/architecture.md) for the original design rationale and data flow.

## Deployment examples

The original deployment used Linux service scheduling. Sanitized examples are included under `deployment/`:

- a hardened systemd service for the long-running connection cleaner;
- a cron entry using `flock` to prevent overlapping monitor jobs.

Adjust paths, users and secret-loading to match your system before use.

## Safety notes

Connection reset is a destructive network action. Start with `--dry-run`, keep `allowed_clear_ranges` narrow, use exception addresses, and review the iKuai API behavior on your own firmware version before enabling unattended operation.

TLS verification is configurable because some homelab routers use self-signed certificates. Prefer a trusted certificate and `verify_tls: true` where possible.

## Sanitization note

This public portfolio version does **not** contain the original router password, notification credentials, PPPoE account identifiers, internal machine labels, production file paths, or the original site-specific addressing plan.

## 中文简介

这是一个从真实家庭实验室运维脚本整理而来的 **iKuai 网络监控与自动化项目**。原部署已停用，本仓库作为脱敏后的求职/作品集版本保留，重点展示：iKuai API 调用、状态持久化、低上传连接清理策略、多重安全保护、线路与 LAN 设备监控、异常/恢复通知，以及 systemd/cron 自动化部署思路。

公开版本已移除真实账号、密码、PPPoE 账号标识、内部设备名称、生产路径和实际网络拓扑信息。
