"""
账号级代理统一工厂（阶段D）。

所有面向闲鱼的出站客户端必须从这里构建，禁止直连。
设计对齐 .trellis/tasks/11-03-account-proxy-fingerprint-isolation/design.md §6。

scheme 翻译(同一 'socks5' 在不同库语义不同, 必须按目标库翻译):
  - httpx            -> 'socks5h://'(h = 代理侧解析 DNS, 禁止本机解析)
  - aiohttp(python-socks) -> 'socks5://'(parse_proxy_url 默认 rdns=True)
  - playwright/chrome -> 'socks5://'(Chrome 自行远端解析)

fail-closed(R1.7): 账号已配置代理但不可用时必须停连+告警, 禁止降级直连。
trust_env=False(R1.5): 显式代理的客户端禁止读机器环境变量(避免 v2rayN 127.0.0.1:7890 覆盖)。
"""
from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import quote

from loguru import logger


class ProxyConfigError(Exception):
    """代理配置错误(已配置但不可用, fail-closed 触发)。"""


def _build_auth(host: str, port: int, user: Optional[str], pwd: Optional[str]) -> str:
    auth = ""
    if user:
        auth = quote(str(user), safe="")
        if pwd:
            auth += f":{quote(str(pwd), safe='')}"
        auth += "@"
    return f"{auth}{host}:{port}"


def httpx_proxy_url(proxy_type: str, host: str, port: int,
                    user: Optional[str] = None, pwd: Optional[str] = None) -> str:
    """返回 httpx 可用的代理 URL。SOCKS5 强制 'socks5h://'(代理侧解析 DNS)。"""
    ptype = (proxy_type or "none").lower()
    if ptype in ("none", ""):
        return ""
    if ptype == "socks5":
        return f"socks5h://{_build_auth(host, port, user, pwd)}"
    if ptype in ("http", "https"):
        return f"{ptype}://{_build_auth(host, port, user, pwd)}"
    raise ProxyConfigError(f"未知代理类型: {proxy_type}")


def aiohttp_proxy_url(proxy_type: str, host: str, port: int,
                      user: Optional[str] = None, pwd: Optional[str] = None) -> str:
    """返回 aiohttp/python-socks 可用的代理 URL。SOCKS5 用 'socks5://'(默认 rdns=True)。"""
    ptype = (proxy_type or "none").lower()
    if ptype in ("none", ""):
        return ""
    if ptype == "socks5":
        return f"socks5://{_build_auth(host, port, user, pwd)}"
    if ptype in ("http", "https"):
        return f"{ptype}://{_build_auth(host, port, user, pwd)}"
    raise ProxyConfigError(f"未知代理类型: {proxy_type}")


def playwright_proxy(proxy_type: str, host: str, port: int,
                     user: Optional[str] = None, pwd: Optional[str] = None) -> Optional[Dict[str, str]]:
    """返回 Playwright launch 的 proxy dict; 未配置代理返回 None。"""
    ptype = (proxy_type or "none").lower()
    if ptype in ("none", ""):
        return None
    if ptype not in ("http", "https", "socks5"):
        raise ProxyConfigError(f"未知代理类型: {proxy_type}")
    server = f"{ptype}://{host}:{port}"
    result: Dict[str, str] = {"server": server}
    if user:
        result["username"] = str(user)
    if pwd:
        result["password"] = str(pwd)
    return result


def chrome_proxy_arg(proxy_type: str, host: str, port: int,
                     user: Optional[str] = None, pwd: Optional[str] = None) -> str:
    """返回 Chrome --proxy-server 参数值。Chrome 对 SOCKS5 自行远端解析。"""
    ptype = (proxy_type or "none").lower()
    if ptype in ("none", ""):
        return ""
    if ptype == "socks5":
        return f"socks5://{host}:{port}"
    if ptype in ("http", "https"):
        return f"{ptype}://{host}:{port}"
    raise ProxyConfigError(f"未知代理类型: {proxy_type}")


def webrtc_policy_args() -> list[str]:
    """封堵 WebRTC 暴露本机网卡 IP(R1.8)。所有浏览器启动点必须附加。"""
    return ["--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]


def require_proxy(proxy_type: Optional[str], host: Optional[str], port: Optional[int],
                  context: str = "") -> None:
    """fail-closed 校验: 已配置代理但缺关键字段时抛 ProxyConfigError, 禁止降级直连。"""
    ptype = (proxy_type or "none").lower()
    if ptype in ("none", ""):
        return
    missing = []
    if not host:
        missing.append("host")
    if not port:
        missing.append("port")
    if missing:
        raise ProxyConfigError(
            f"[fail-closed] 账号已配置 {ptype} 代理但缺 {','.join(missing)}, 拒绝直连 {context}"
        )


def resolve_proxy_fields(account: Any) -> Dict[str, Any]:
    """从 XYAccount ORM 对象提取代理字段(容忍属性缺失)。"""
    return {
        "proxy_type": getattr(account, "proxy_type", None),
        "proxy_host": getattr(account, "proxy_host", None),
        "proxy_port": getattr(account, "proxy_port", None),
        "proxy_user": getattr(account, "proxy_user", None),
        "proxy_pass": getattr(account, "proxy_pass", None),
    }
