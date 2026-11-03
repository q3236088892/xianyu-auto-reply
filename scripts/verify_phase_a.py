"""阶段A 验证脚本: 单一来源访问器的正确性自检。
只读, 不启动服务, 直接调用 common.utils.xianyu_utils 的访问器断言。
"""
from common.utils.xianyu_utils import (
    BROWSER_ACCEPT_LANGUAGE,
    BROWSER_LOCALE,
    BROWSER_SCREEN,
    BROWSER_TIMEZONE_ID,
    browser_chrome_major,
    browser_chrome_version,
    browser_client_hints_metadata,
    browser_sec_ch_ua,
    browser_user_agent,
)

print("=" * 70)
print("阶段A 自检: browser identity 单一来源")
print("=" * 70)

version = browser_chrome_version()
major = browser_chrome_major()
ua = browser_user_agent()
sec_ch_ua = browser_sec_ch_ua()
metadata = browser_client_hints_metadata()

print(f"\n探测到的完整版本 : {version}")
print(f"主版本           : {major}")
print(f"UA               : {ua}")
print(f"sec-ch-ua        : {sec_ch_ua}")
print(f"常量 accept-lang : {BROWSER_ACCEPT_LANGUAGE}")
print(f"常量 locale      : {BROWSER_LOCALE}")
print(f"常量 timezone    : {BROWSER_TIMEZONE_ID}")
print(f"常量 screen      : {BROWSER_SCREEN}")

print("\n--- 断言 ---")
assert ua == (
    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    f"AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{major}.0.0.0 Safari/537.36"
), "UA 未用 major.0.0.0"
print("✓ UA 使用 major.0.0.0")

assert f'Chrome/{major}.0.0.0' in ua, "UA 里必须只有主版本"
assert version.split(".", 1)[0] == str(major), "major 与完整版本不一致"
print("✓ UA 只含主版本, 完整版本一致")

assert sec_ch_ua == (
    f'"Google Chrome";v="{major}", '
    f'"Chromium";v="{major}", '
    f'"Not(A:Brand";v="24"'
), "sec-ch-ua 品牌顺序错误"
print("✓ sec-ch-ua 品牌顺序 = Google Chrome -> Chromium -> Not(A:Brand);v=24")

assert metadata["fullVersion"] == version, "metadata.fullVersion 必须是完整版本"
assert metadata["brands"][0] == {"brand": "Google Chrome", "version": str(major)}
assert metadata["brands"][2] == {"brand": "Not(A:Brand", "version": "24"}
print("✓ client hints metadata.fullVersion = 完整版本(补丁号只出现在这里)")

# 与真实 Chrome 抓包对比(2026-11-03, Chrome 155)
print("\n--- 与真实 Chrome 抓包对比 ---")
expected_major = 155
if major == expected_major:
    print(f"✓ 主版本 {major} == 线上 Chrome 大版本 {expected_major}")
else:
    print(f"! 主版本 {major} != 线上 {expected_major}(内核可能已升级, 属正常)")

print("\n=== 全部通过 ===")
