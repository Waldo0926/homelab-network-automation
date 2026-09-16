from __future__ import annotations

import hashlib
import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)


class IkuaiAPIError(RuntimeError):
    """Raised when the iKuai API returns an error."""


class IkuaiClient:
    """Small client for the iKuai /Action/login and /Action/call endpoints."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        device_name: str | None = None,
        verify_tls: bool = False,
        timeout: tuple[int, int] = (10, 30),
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.device_name = device_name or self.base_url
        self.verify_tls = verify_tls
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"content-type": "application/json"})

    @staticmethod
    def _md5(text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    def login(self) -> None:
        payload = {
            "username": self.username,
            "passwd": self._md5(self.password),
            "pass": "",
            "remember_password": "0",
        }
        response = self.session.post(
            f"{self.base_url}/Action/login",
            json=payload,
            verify=self.verify_tls,
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("ErrMsg") != "Success":
            raise IkuaiAPIError(f"[{self.device_name}] login failed: {data.get('ErrMsg')}")

    def call(self, func_name: str, action: str, param: dict[str, Any]) -> Any:
        payload = {"func_name": func_name, "action": action, "param": param}
        response = self.session.post(
            f"{self.base_url}/Action/call",
            json=payload,
            verify=self.verify_tls,
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("ErrMsg") != "Success":
            raise IkuaiAPIError(
                f"[{self.device_name}] {func_name}/{action} failed: {data.get('ErrMsg')}"
            )
        return data.get("Data", {})

    def monitor_lanip(self, limit: int = 300) -> list[dict[str, Any]]:
        data = self.call(
            "monitor_lanip",
            "show",
            {
                "TYPE": "data,total",
                "ORDER_BY": "ip_addr_int",
                "orderType": "IP",
                "limit": f"0,{limit}",
                "ORDER": "",
            },
        )
        return data.get("data", []) if isinstance(data, dict) else []

    def wan(self, limit: int = 200) -> list[dict[str, Any]]:
        data = self.call(
            "wan",
            "show",
            {"TYPE": "data,total", "limit": f"0,{limit}", "ORDER": "", "ORDER_BY": "id"},
        )
        return data.get("data", []) if isinstance(data, dict) else []

    def dhcp_lease(self, limit: int = 300) -> list[dict[str, Any]]:
        data = self.call(
            "dhcp_lease",
            "show",
            {
                "TYPE": "data,total",
                "ORDER_BY": "ip_addr_int",
                "orderType": "IP",
                "limit": f"0,{limit}",
                "ORDER": "",
            },
        )
        return data.get("data", []) if isinstance(data, dict) else []

    def dhcp_static(self, limit: int = 300) -> list[dict[str, Any]]:
        data = self.call(
            "dhcp_static",
            "show",
            {
                "TYPE": "data,total",
                "ORDER_BY": "ip_addr_int",
                "orderType": "IP",
                "limit": f"0,{limit}",
                "ORDER": "",
            },
        )
        return data.get("data", []) if isinstance(data, dict) else []

    def clear_connections(self, ip_addr: str) -> bool:
        result = self.call("monitor_lanip", "del_conn", {"ip": ip_addr})
        return bool(result)
