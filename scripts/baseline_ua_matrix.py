#!/usr/bin/env python3
"""阶段0基线: 只读扫描各出站路径的 UA / client-hints 矩阵。
不启动任何服务、不发起任何网络请求, 纯静态提取常量与调用点。
输出到 stdout, 便于与阶段A改造后对比。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 候选文件 -> 期望命中的 UA 特征
CANDIDATES = {
    "backend-web/app/services/qr_login/manager.py": "扫码登录",
    "common/services/xianyu_mtop.py": "mtop",
    "common/services/xianyu_login/login_do.py": "密码登录",
    "common/services/xianyu_login/face_verification.py": "人脸验证",
    "common/services/im_token_api.py": "IM Token",
    "common/services/captcha/playwright_touch.py": "滑块UA覆写",
    "common/services/captcha/slider_stealth.py": "滑块stealth",
    "common/services/cookie_renew_browser_service.py": "Cookie续期浏览器",
    "websocket/app/services/xianyu/xianyu_async.py": "WebSocket长连",
    "websocket/app/services/xianyu/cookies_refresh_service.py": "Cookie刷新",
}

UA_RE = re.compile(r"Mozilla/5\.0[^\"'\n]{0,220}")
SEC_CH_UA_RE = re.compile(r"sec-ch-ua[\"']?\s*[:=]\s*[\"']([^\"']+)[\"']")
UA_HARDCODE_RE = re.compile(r"(Chrome/[\d.]+)")


def extract(path: Path) -> dict:
    rel = path.relative_to(ROOT).as_posix()
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:  # noqa: BLE001
        return {"file": rel, "error": str(exc)}
    uas = [m.group(0) for m in UA_RE.finditer(text)]
    sec_ch_ua = [m.group(1) for m in SEC_CH_UA_RE.finditer(text)]
    chrome_ver = sorted(set(UA_HARDCODE_RE.findall(text)))
    return {
        "file": rel,
        "硬编码Chrome版本": chrome_ver or "-",
        "完整UA条数": len(uas),
        "UA样本(前2)": uas[:2],
        "sec-ch-ua样本": sec_ch_ua[:3],
    }


def main() -> None:
    print("=" * 78)
    print("阶段0 基线: 出站路径 UA / client-hints 矩阵(只读)")
    print("=" * 78)
    versions_seen: dict[str, list[str]] = {}
    for rel, label in CANDIDATES.items():
        path = ROOT / rel
        if not path.exists():
            print(f"\n[!] 缺失 {rel} ({label})")
            continue
        info = extract(path)
        print(f"\n--- {label} ---")
        for k, v in info.items():
            if k == "file":
                print(f"  {k}: {v}")
            else:
                print(f"  {k}: {v}")
        for ver in info.get("硬编码Chrome版本", []) or []:
            versions_seen.setdefault(ver, []).append(rel)

    print("\n" + "=" * 78)
    print("汇总: 硬编码 Chrome 版本 -> 出现位置")
    for ver, files in sorted(versions_seen.items()):
        print(f"\n  {ver}")
        for f in files:
            print(f"    - {f}")


if __name__ == "__main__":
    main()
