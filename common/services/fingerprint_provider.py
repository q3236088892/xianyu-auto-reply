"""
账号级浏览器指纹档案（阶段C）。

设计原则（对齐 .trellis/tasks/11-03-account-proxy-fingerprint-isolation/design.md §4）:
- 每账号一次性生成、永久固定、可复现（种子可回溯）、账号间互不重复
- 存储在 xy_accounts.metadata 的 'fingerprint' 键（JSON 列已存在）
- profile 建立后不重建；重建 = 给闲鱼换设备
- 同机最多 2-3 账号: 通过屏幕/时区/语言/locale + UA-consistent 做差异化，
  不上 canvas/WebGL 噪声（避免与真硬件矛盾, 见 PRD Risks 同机物理共性）

消费层（阶段A已统一 UA 来源）:
  本 provider 在阶段A的 browser_user_agent 基础上, 叠加"每账号不同"的维度
  (屏幕/时区/语言), 由启动参数层的 browser_features 消费。

种子规则:
  fingerprint_seed = SHA256(f"{account_id}:{_SEED_NAMESPACE}")[:16]
  无需密钥保管, 可公开回溯; 账号ID本身已是稳定唯一标识。
"""
from __future__ import annotations

import hashlib
import random
from typing import Any, Dict

# 与 xianyu_utils 的单一来源对齐(阶段A), 不得另行硬编码
from common.utils.xianyu_utils import browser_user_agent

_SEED_NAMESPACE = "xianyu-fingerprint-v1"
_FP_KEY = "fingerprint"

# 候选池: 国内常见桌面分辨率 + 与之自洽的常见缩放比
_SCREEN_POOL = [
    {"width": 1920, "height": 1080, "device_scale_factor": 1.0},
    {"width": 1920, "height": 1080, "device_scale_factor": 1.25},
    {"width": 1536, "height": 864, "device_scale_factor": 1.25},
    {"width": 1366, "height": 768, "device_scale_factor": 1.0},
    {"width": 2560, "height": 1440, "device_scale_factor": 1.0},
    {"width": 1600, "height": 900, "device_scale_factor": 1.0},
]

# 国内时区(单一国家, 与代理geo锚定解耦——代理尚未采购, 阶段C先给独立时区)
_TZ_POOL = [
    "Asia/Shanghai",
    "Asia/Shanghai",
    "Asia/Shanghai",   # 大概率落在上海(主流), 但保留多样性
    "Asia/Harbin",
    "Asia/Chongqing",
    "Asia/Urumqi",
]

_ACCEPT_LANG_POOL = [
    "zh-CN,zh;q=0.9",
    "zh-CN,zh;q=0.9,en;q=0.8",
    "zh-CN,zh;q=0.8,en;q=0.7",
    "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
]

_LOCALE_POOL = ["zh-CN", "zh-CN", "zh-CN", "zh-Hans-CN"]


def _account_seed(account_id: str) -> str:
    """由账号ID派生稳定种子(可公开回溯, 无需密钥)。"""
    digest = hashlib.sha256(f"{account_id}:{_SEED_NAMESPACE}".encode("utf-8")).hexdigest()
    return digest[:16]


def generate_fingerprint(account_id: str) -> Dict[str, Any]:
    """每账号一次性生成指纹档案。

    返回的 dict 应原样存入 metadata['fingerprint'], 之后只读。
    相同 account_id 永远生成相同档案(种子可回溯, 账号间互不重复)。
    """
    seed_hex = _account_seed(str(account_id))
    rng = random.Random(seed_hex)

    screen = rng.choice(_SCREEN_POOL)
    fp = {
        "version": 1,
        "seed": seed_hex,
        "ua": browser_user_agent(),   # 单一来源, 随 Chrome 自动升级
        "screen_width": screen["width"],
        "screen_height": screen["height"],
        "device_scale_factor": screen["device_scale_factor"],
        "viewport_width": screen["width"],
        "viewport_height": screen["height"],
        "timezone_id": rng.choice(_TZ_POOL),
        "locale": rng.choice(_LOCALE_POOL),
        "accept_language": rng.choice(_ACCEPT_LANG_POOL),
        "hardware_concurrency": rng.choice([4, 6, 8, 8, 12, 16]),
        "device_memory": rng.choice([4, 8, 8, 16]),
    }
    return fp


def get_account_fingerprint(metadata_json: Dict[str, Any] | None) -> Dict[str, Any] | None:
    """从 xy_accounts.metadata JSON 读取已存指纹档案。

    未生成则返回 None(调用方应触发 generate_fingerprint + 回写)。
    本函数不隐式生成, 避免运行期随机膨胀 metadata。
    """
    if not metadata_json:
        return None
    fp = metadata_json.get(_FP_KEY)
    if isinstance(fp, dict) and fp.get("version") == 1:
        return fp
    return None


def fingerprint_digest(fp: Dict[str, Any]) -> str:
    """指纹稳定摘要(用于登录日志记录与账号间去重校验)。"""
    if not fp:
        return ""
    canonical = "|".join(str(fp.get(k, "")) for k in sorted(fp.keys()) if k != "ua")
    ua = fp.get("ua", "")
    return hashlib.sha256(f"{canonical}|{ua}".encode("utf-8")).hexdigest()[:16]
