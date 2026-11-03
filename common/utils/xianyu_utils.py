"""
闲鱼工具函数

包含加密解密、签名生成、Cookie解析等核心功能
完全复刻原始 utils/xianyu_utils.py 的逻辑
"""
from __future__ import annotations

import base64
import functools
import hashlib
import json
import os
import random
import struct
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from loguru import logger


# ============================================================================
# 浏览器身份单一来源 (阶段 A: 统一 UA / client hints)
# ============================================================================
# 所有面向闲鱼的出站请求(扫码/密码/人脸/mtop/IM/WebSocket/浏览器)必须从
# 这里的访问器取 UA 与 client hints, 禁止再写字面量。探测结果 lru_cache
# 缓存, 进程生命周期内固定, 保证同一账号任意时刻自洽。
#
# 版本探测优先级(真实可信度的代理):
#   1. BLBeacon.version           —— Chrome 自报当前真实版本(最准)
#   2. User Data/Last Version     —— Chrome 最后一次写入磁盘的版本
#   3. 已安装 playwright chromium —— 滑块 stealth 实际用的内核
#   4. _FALLBACK_CHROME_VERSION   —— 兜底(必须与线上稳定版大版本接近)
# ============================================================================

# 品牌顺序与占位版本 "Not(A:Brand";v="24" 已按真实 Chrome 抓包校准(2026-11-03,
# Chrome 155, sec-ch-ua: "Google Chrome";v="155", "Chromium";v="155", "Not(A:Brand";v="24")
_FALLBACK_CHROME_VERSION = "155.0.8059.85"
_UA_TEMPLATE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/{major}.0.0.0 Safari/537.36"
)


def _read_blbeacon_version() -> str | None:
    """从注册表 HKCU\\Software\\Google\\Chrome\\BLBeacon 读 Chrome 自报版本。"""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon") as key:
            value, _ = winreg.QueryValueEx(key, "version")
        value = str(value).strip()
        return value or None
    except Exception:
        return None


def _read_last_version() -> str | None:
    """读 Chrome User Data 的 Last Version 文件(记录最后写盘的版本)。"""
    candidates = []
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(Path(local_appdata) / "Google" / "Chrome" / "User Data" / "Last Version")
    home = os.environ.get("USERPROFILE")
    if home:
        candidates.append(Path(home) / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "Last Version")
    for path in candidates:
        try:
            text = path.read_text(encoding="ascii", errors="ignore").strip()
            if text:
                return text
        except Exception:
            continue
    return None


def _search_playwright_chromium(root: Path) -> str | None:
    """在 playwright 缓存里找最高的 chromium-NNNN 目录, 用其版本文件作为代理。"""
    try:
        best_revision = -1
        best_path: Path | None = None
        for child in root.iterdir():
            if not child.is_dir() or not child.name.startswith("chromium-"):
                continue
            suffix = child.name.removeprefix("chromium-")
            if not suffix.isdigit():
                continue
            revision = int(suffix)
            if revision > best_revision:
                best_revision = revision
                best_path = child
        if best_path is None:
            return None
        for candidate in (
            best_path / "chrome-win64" / "version.txt",
            best_path / "version.txt",
        ):
            if candidate.is_file():
                text = candidate.read_text(encoding="ascii", errors="ignore").strip()
                if text:
                    return text
    except Exception:
        return None
    return None


def _read_playwright_chromium_version() -> str | None:
    """探测项目内嵌的 playwright chromium 版本(作为未装 Chrome 时的代理)。"""
    roots = []
    portable = Path(__file__).resolve().parents[2]
    roots.append(portable / "runtime" / "playwright")
    for env_var in ("PLAYWRIGHT_BROWSERS_PATH",):
        value = os.environ.get(env_var)
        if value:
            roots.append(Path(value))
    home = os.environ.get("USERPROFILE")
    if home:
        roots.append(Path(home) / "AppData" / "Local" / "ms-playwright")
    for root in roots:
        version = _search_playwright_chromium(root)
        if version:
            return version
    return None


@functools.lru_cache(maxsize=1)
def _detect_chrome_version() -> str:
    """按优先级探测 Chrome 完整版本(如 '155.0.8059.85'), 只探测一次。"""
    for name, probe in (
        ("BLBeacon", _read_blbeacon_version),
        ("Last Version", _read_last_version),
        ("playwright chromium", _read_playwright_chromium_version),
    ):
        try:
            version = probe()
        except Exception:
            version = None
        if version:
            parts = version.split(".")
            if len(parts) >= 2 and parts[0].isdigit():
                logger.info(f"[browser-identity] Chrome 版本来源={name} 版本={version}")
                return version
    logger.warning(
        f"[browser-identity] 无法探测 Chrome 版本, 回退常量 {_FALLBACK_CHROME_VERSION}"
    )
    return _FALLBACK_CHROME_VERSION


def browser_chrome_version() -> str:
    """Chrome 完整版本(如 '155.0.8059.85'), 单一版本源。"""
    return _detect_chrome_version()


def browser_chrome_major() -> int:
    """Chrome 主版本号(如 155)。"""
    return int(_detect_chrome_version().split(".", 1)[0])


def browser_user_agent() -> str:
    """User-Agent, 只用主版本号(Chrome 的 UA 恒为 major.0.0.0)。"""
    return _UA_TEMPLATE.format(major=browser_chrome_major())


def browser_sec_ch_ua() -> str:
    """sec-ch-ua 请求头, 品牌顺序已按真实 Chrome 抓包校准。"""
    major = browser_chrome_major()
    return (
        f'"Google Chrome";v="{major}", '
        f'"Chromium";v="{major}", '
        f'"Not(A:Brand";v="24"'
    )


def browser_client_hints_metadata() -> Dict[str, Any]:
    """CDP Emulation.setUserAgentOverride 的 userAgentMetadata 结构。

    brands 里的 fullVersion 用完整版本(UA 里不会出现补丁号, 只出现在这里),
    与真实 Chrome 的 client hints 行为一致。
    """
    version = _detect_chrome_version()
    major = browser_chrome_major()
    return {
        "brands": [
            {"brand": "Google Chrome", "version": str(major)},
            {"brand": "Chromium", "version": str(major)},
            {"brand": "Not(A:Brand", "version": "24"},
        ],
        "fullVersion": version,
        "platform": "Windows",
        "platformVersion": "10.0.0",
        "architecture": "x86",
        "model": "",
        "mobile": False,
    }


# 统一常量: 所有路径共享同一 locale/时区/屏幕
BROWSER_ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9"
BROWSER_LOCALE = "zh-CN"
BROWSER_TIMEZONE_ID = "Asia/Shanghai"
BROWSER_SCREEN = {"width": 1920, "height": 1080}


CLOSE_NOTICE_API = "mtop.taobao.idlemessage.pc.profile.notice.update"


def trans_cookies(cookies_str: str) -> Dict[str, str]:
    """将cookies字符串转换为字典
    
    Args:
        cookies_str: Cookie字符串，格式如 "key1=value1; key2=value2"
                     兼容 "key1=value1;key2=value2"（分号后无空格）的情况
        
    Returns:
        Cookie字典
        
    Raises:
        ValueError: 如果cookies为空
    """
    if not cookies_str:
        raise ValueError("cookies不能为空")
    
    cookies = {}
    # 按分号分割，兼容 "; " 和 ";" 两种分隔符
    for cookie in cookies_str.split(";"):
        cookie = cookie.strip()
        if not cookie:
            continue
        if "=" in cookie:
            key, value = cookie.split("=", 1)
            key = key.strip()
            if key:
                cookies[key] = value.strip()
    return cookies


def extract_account_user_id_from_cookie(cookies_str: str) -> str:
    """从Cookie中提取当前闲鱼账号标识。"""
    if not cookies_str:
        return ""

    cookie_map: Dict[str, str] = {}
    for cookie in cookies_str.split(";"):
        cookie = cookie.strip()
        if "=" not in cookie:
            continue
        key, value = cookie.split("=", 1)
        cookie_map[key.strip()] = value.strip()

    return str(cookie_map.get("unb") or cookie_map.get("munb") or "").strip()


def generate_mid() -> str:
    """生成消息ID"""
    random_part = int(1000 * random.random())
    timestamp = int(time.time() * 1000)
    return f"{random_part}{timestamp} 0"


def generate_uuid() -> str:
    """生成UUID"""
    timestamp = int(time.time() * 1000)
    return f"-{timestamp}1"


def generate_device_id(user_id: str) -> str:
    """生成设备ID
    
    Args:
        user_id: 用户ID
        
    Returns:
        设备ID字符串
    """
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    result = []
    
    for i in range(36):
        if i in [8, 13, 18, 23]:
            result.append("-")
        elif i == 14:
            result.append("4")
        else:
            if i == 19:
                rand_val = int(16 * random.random())
                result.append(chars[(rand_val & 0x3) | 0x8])
            else:
                rand_val = int(16 * random.random())
                result.append(chars[rand_val])
    
    return ''.join(result) + "-" + user_id


def generate_sign(t: str, token: str, data: str) -> str:
    """生成API签名
    
    Args:
        t: 时间戳
        token: _m_h5_tk token
        data: 请求数据
        
    Returns:
        签名字符串
    """
    app_key = "34839810"
    msg = f"{token}&{t}&{app_key}&{data}"
    
    md5_hash = hashlib.md5()
    md5_hash.update(msg.encode('utf-8'))
    return md5_hash.hexdigest()


async def close_account_notice(account_id: str, cookies_str: str, task_name: str = "关闭账号消息通知") -> tuple[bool, str | None]:
    if not cookies_str:
        return False, "账号Cookie为空"

    import aiohttp

    try:
        cookies = trans_cookies(cookies_str)
    except Exception as e:
        return False, f"Cookie解析失败: {e}"

    timestamp = str(int(time.time() * 1000))
    data_val = '{"oprType":2,"appKeys":["444e9908a51d1cb236a27862abc769c9"]}'

    token = cookies.get("_m_h5_tk", "").split("_")[0] if cookies.get("_m_h5_tk") else ""
    sign = generate_sign(timestamp, token, data_val)

    params = {
        "jsv": "2.7.2",
        "appKey": "34839810",
        "t": timestamp,
        "sign": sign,
        "v": "1.0",
        "type": "originaljson",
        "accountSite": "xianyu",
        "dataType": "json",
        "timeout": "20000",
        "api": CLOSE_NOTICE_API,
        "sessionOption": "AutoLoginOnly",
    }

    headers = {
        "accept": "application/json",
        "content-type": "application/x-www-form-urlencoded",
        "cookie": cookies_str,
        "Referer": "https://www.goofish.com/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/138.0.0.0 Safari/537.36"
        ),
    }

    url = f"https://h5api.m.goofish.com/h5/{CLOSE_NOTICE_API}/1.0/"

    try:
        async with aiohttp.ClientSession() as http_session:
            async with http_session.post(
                url,
                params=params,
                data={"data": data_val},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                try:
                    res_json = await response.json(content_type=None)
                except Exception:
                    text = await response.text()
                    return False, f"响应解析失败: {text[:200]}"

                ret = res_json.get("ret", [])
                ret_str = ret[0] if ret else ""

                logger.info(f"【{task_name}】账号 {account_id} 接口完整返回: {res_json}")

                if "SUCCESS" in ret_str:
                    data = res_json.get("data", {})
                    if data.get("success") is True:
                        return True, None
                    return False, f"接口返回success=false: {data}"

                return False, ret_str or "未知错误"

    except aiohttp.ClientError as e:
        return False, f"网络请求失败: {e}"
    except Exception as e:
        return False, f"请求异常: {e}"


class MessagePackDecoder:
    """MessagePack解码器的纯Python实现"""
    
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.length = len(data)
    
    def read_byte(self) -> int:
        if self.pos >= self.length:
            raise ValueError("Unexpected end of data")
        byte = self.data[self.pos]
        self.pos += 1
        return byte
    
    def read_bytes(self, count: int) -> bytes:
        if self.pos + count > self.length:
            raise ValueError("Unexpected end of data")
        result = self.data[self.pos:self.pos + count]
        self.pos += count
        return result
    
    def read_uint8(self) -> int:
        return self.read_byte()
    
    def read_uint16(self) -> int:
        return struct.unpack('>H', self.read_bytes(2))[0]
    
    def read_uint32(self) -> int:
        return struct.unpack('>I', self.read_bytes(4))[0]
    
    def read_uint64(self) -> int:
        return struct.unpack('>Q', self.read_bytes(8))[0]
    
    def read_int8(self) -> int:
        return struct.unpack('>b', self.read_bytes(1))[0]
    
    def read_int16(self) -> int:
        return struct.unpack('>h', self.read_bytes(2))[0]
    
    def read_int32(self) -> int:
        return struct.unpack('>i', self.read_bytes(4))[0]
    
    def read_int64(self) -> int:
        return struct.unpack('>q', self.read_bytes(8))[0]
    
    def read_float32(self) -> float:
        return struct.unpack('>f', self.read_bytes(4))[0]
    
    def read_float64(self) -> float:
        return struct.unpack('>d', self.read_bytes(8))[0]
    
    def read_string(self, length: int) -> str:
        return self.read_bytes(length).decode('utf-8')
    
    def decode_value(self) -> Any:
        """解码单个MessagePack值"""
        if self.pos >= self.length:
            raise ValueError("Unexpected end of data")
            
        format_byte = self.read_byte()
        
        # Positive fixint (0xxxxxxx)
        if format_byte <= 0x7f:
            return format_byte
        
        # Fixmap (1000xxxx)
        elif 0x80 <= format_byte <= 0x8f:
            size = format_byte & 0x0f
            return self.decode_map(size)
        
        # Fixarray (1001xxxx)
        elif 0x90 <= format_byte <= 0x9f:
            size = format_byte & 0x0f
            return self.decode_array(size)
        
        # Fixstr (101xxxxx)
        elif 0xa0 <= format_byte <= 0xbf:
            size = format_byte & 0x1f
            return self.read_string(size)
        
        # nil
        elif format_byte == 0xc0:
            return None
        
        # false
        elif format_byte == 0xc2:
            return False
        
        # true
        elif format_byte == 0xc3:
            return True
        
        # bin 8
        elif format_byte == 0xc4:
            size = self.read_uint8()
            return self.read_bytes(size)
        
        # bin 16
        elif format_byte == 0xc5:
            size = self.read_uint16()
            return self.read_bytes(size)
        
        # bin 32
        elif format_byte == 0xc6:
            size = self.read_uint32()
            return self.read_bytes(size)
        
        # float 32
        elif format_byte == 0xca:
            return self.read_float32()
        
        # float 64
        elif format_byte == 0xcb:
            return self.read_float64()
        
        # uint 8
        elif format_byte == 0xcc:
            return self.read_uint8()
        
        # uint 16
        elif format_byte == 0xcd:
            return self.read_uint16()
        
        # uint 32
        elif format_byte == 0xce:
            return self.read_uint32()
        
        # uint 64
        elif format_byte == 0xcf:
            return self.read_uint64()
        
        # int 8
        elif format_byte == 0xd0:
            return self.read_int8()
        
        # int 16
        elif format_byte == 0xd1:
            return self.read_int16()
        
        # int 32
        elif format_byte == 0xd2:
            return self.read_int32()
        
        # int 64
        elif format_byte == 0xd3:
            return self.read_int64()
        
        # str 8
        elif format_byte == 0xd9:
            size = self.read_uint8()
            return self.read_string(size)
        
        # str 16
        elif format_byte == 0xda:
            size = self.read_uint16()
            return self.read_string(size)
        
        # str 32
        elif format_byte == 0xdb:
            size = self.read_uint32()
            return self.read_string(size)
        
        # array 16
        elif format_byte == 0xdc:
            size = self.read_uint16()
            return self.decode_array(size)
        
        # array 32
        elif format_byte == 0xdd:
            size = self.read_uint32()
            return self.decode_array(size)
        
        # map 16
        elif format_byte == 0xde:
            size = self.read_uint16()
            return self.decode_map(size)
        
        # map 32
        elif format_byte == 0xdf:
            size = self.read_uint32()
            return self.decode_map(size)
        
        # Negative fixint (111xxxxx)
        elif format_byte >= 0xe0:
            return format_byte - 0x100
        
        raise ValueError(f"Unknown format byte: {format_byte:02x}")

    def decode_array(self, size: int) -> List[Any]:
        """解码数组"""
        return [self.decode_value() for _ in range(size)]

    def decode_map(self, size: int) -> Dict[Any, Any]:
        """解码字典"""
        result = {}
        for _ in range(size):
            key = self.decode_value()
            value = self.decode_value()
            result[key] = value
        return result

    def decode(self) -> Any:
        """解码整个MessagePack数据"""
        return self.decode_value()


def decrypt(data: str) -> str:
    """解密消息数据
    
    Args:
        data: Base64编码的MessagePack数据
        
    Returns:
        解密后的JSON字符串
        
    Raises:
        Exception: 解密失败时抛出异常
    """
    try:
        if not isinstance(data, str):
            data = str(data)

        # 清理数据
        try:
            data.encode('ascii')
        except UnicodeEncodeError:
            data = data.encode('utf-8', errors='ignore').decode('ascii', errors='ignore')

        # Base64解码
        try:
            decoded_data = base64.b64decode(data)
        except Exception:
            missing_padding = len(data) % 4
            if missing_padding:
                data += '=' * (4 - missing_padding)
            decoded_data = base64.b64decode(data)

        # MessagePack解码
        decoder = MessagePackDecoder(decoded_data)
        decoded_value = decoder.decode()

        # 转换为JSON字符串
        if isinstance(decoded_value, dict):
            def json_serializer(obj):
                if isinstance(obj, bytes):
                    return obj.decode('utf-8', errors='ignore')
                raise TypeError(f"Type {type(obj)} not serializable")

            return json.dumps(decoded_value, default=json_serializer, ensure_ascii=False)

        return str(decoded_value)

    except Exception as e:
        raise Exception(f"解密失败: {str(e)}")
