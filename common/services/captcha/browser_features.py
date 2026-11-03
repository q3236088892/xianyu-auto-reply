"""
浏览器特征和反检测脚本

提供 Playwright 滑块浏览器特征和最小化自动化标记隐藏脚本。

原则：尽量保留 Chromium 原生值。伪造 UA、屏幕、插件、Canvas、WebGL 或时间 API
容易产生互相矛盾的指纹，实际比只隐藏明确的自动化标记更容易触发风控。

阶段C: 新增 get_account_browser_features(account_id, metadata_json),
        返回"每账号固定"的浏览器特征(屏幕/时区/语言), 账号间互不重复。
        原 get_random_browser_features 保留向后兼容, 内部走同一固定档案。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from common.services.fingerprint_provider import (
    generate_fingerprint,
    get_account_fingerprint,
)


def get_random_browser_features() -> Dict[str, Any]:
    """向后兼容: 返回无账号维度的稳定特征配置(等同固定档案默认)。

    新代码应改用 get_account_browser_features(account_id, metadata_json)。
    """
    return {
        'window_size': '1920,1080',
        'lang': 'zh-CN',
        'accept_lang': 'zh-CN,zh;q=0.9,en;q=0.8',
        'locale': 'zh-CN',
        'viewport_width': 1920,
        'viewport_height': 1080,
        'device_scale_factor': 1.0,
        'is_mobile': False,
        'has_touch': False,
        'timezone_id': 'Asia/Shanghai'
    }


def get_account_browser_features(
    account_id: str,
    metadata_json: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """返回指定账号的固定浏览器特征(账号间互不重复)。

    优先读 metadata['fingerprint'](已生成并回写); 未生成则用 account_id
    派生稳定档案(不隐式回写, 由调用方决定何时固化)。

    返回结构对齐 get_random_browser_features, 供 slider_stealth 启动参数消费。
    """
    fp = get_account_fingerprint(metadata_json) if metadata_json else None
    if fp is None:
        fp = generate_fingerprint(account_id)

    return {
        'window_size': f"{fp['screen_width']},{fp['screen_height']}",
        'lang': fp['locale'],
        'accept_lang': fp['accept_language'],
        'locale': fp['locale'],
        'viewport_width': fp['viewport_width'],
        'viewport_height': fp['viewport_height'],
        'device_scale_factor': fp['device_scale_factor'],
        'is_mobile': False,
        'has_touch': False,
        'timezone_id': fp['timezone_id'],
    }


def get_stealth_script(browser_features: Dict[str, Any]) -> str:
    """返回最小化隐藏脚本，保留浏览器原生指纹与时间行为。"""
    _ = browser_features
    return """
        try {
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
                configurable: true
            });
            delete Object.getPrototypeOf(navigator).webdriver;
        } catch (e) {}
        try {
            delete window.playwright;
            delete window.__playwright;
            delete window.__pw_manual;
            delete window.__PW_inspect;
        } catch (e) {}
    """

