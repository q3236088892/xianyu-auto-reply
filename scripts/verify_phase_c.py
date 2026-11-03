"""阶段C 验证: 指纹矩阵自检。

断言(PRD Acceptance Criteria):
  1. 同账号重复采样 N 次结果稳定(种子固定)
  2. 跨账号两两不相同
  3. real_mouse 每账号独立 browser_dir
  4. slider_stealth 通过 provider 拿到账号差异化特征
  5. 指纹摘要可计算(供登录日志)
"""
import os
os.environ['BROWSER_HEADLESS'] = 'true'

from common.services.fingerprint_provider import (
    generate_fingerprint,
    get_account_fingerprint,
    fingerprint_digest,
)
from common.services.captcha.browser_features import (
    get_account_browser_features,
    get_random_browser_features,
)
from common.services.captcha.real_mouse_slider import _get_shared_solver

print()
print('=' * 70)
print('阶段C 验证: 指纹矩阵')
print('=' * 70)

# --- 1. 同账号稳定 ---
print('\n--- 同账号重复采样稳定性 ---')
acc_a = "2232648246"
samples = [generate_fingerprint(acc_a) for _ in range(5)]
first = samples[0]
identical = all(s == first for s in samples)
print(f'账号 {acc_a} 采样 5 次, 完全一致: {identical}')
assert identical, '同账号采样不一致!'
digest = fingerprint_digest(first)
print(f'指纹摘要: {digest}')
print('  PASS 同账号稳定')

# --- 2. 跨账号两两不同 ---
print('\n--- 跨账号两两不同 ---')
accounts = ["2232648246", "test_acc_2", "test_acc_3", "test_acc_4", "test_acc_5"]
fps = {acc: generate_fingerprint(acc) for acc in accounts}
digests = {acc: fingerprint_digest(fp) for acc, fp in fps.items()}
collision = len(set(digests.values())) != len(digests)
print(f'账号数: {len(accounts)}, 唯一摘要数: {len(set(digests.values()))}')
for acc, d in digests.items():
    fp = fps[acc]
    print(f'  {acc}: digest={d} screen={fp["screen_width"]}x{fp["screen_height"]} dsf={fp["device_scale_factor"]} tz={fp["timezone_id"]} hw={fp["hardware_concurrency"]}/{fp["device_memory"]}GB')
assert not collision, f'摘要碰撞: {digests}'
print('  PASS 跨账号两两不同')

# 关键差异化维度抽查(同机 2-3 账号场景)
screens = {(fp["screen_width"], fp["screen_height"], fp["device_scale_factor"]) for fp in fps.values()}
tzs = {fp["timezone_id"] for fp in fps.values()}
hws = {(fp["hardware_concurrency"], fp["device_memory"]) for fp in fps.values()}
print(f'屏幕组合 {len(screens)} 种, 时区 {len(tzs)} 种, 硬件组合 {len(hws)} 种')

# --- 3. get_account_fingerprint 从 metadata 读取 ---
print('\n--- metadata 读取 ---')
metadata_with_fp = {"fingerprint": first, "other": "x"}
metadata_none = None
assert get_account_fingerprint(metadata_with_fp) == first
assert get_account_fingerprint(metadata_none) is None
assert get_account_fingerprint({}) is None
print('  PASS get_account_fingerprint 读取/边界')

# --- 4. browser_features 账号差异化 ---
print('\n--- browser_features 差异化 ---')
bf_a = get_account_browser_features("2232648246")
bf_b = get_account_browser_features("test_acc_9")
print(f'  账号A features: window={bf_a["window_size"]} tz={bf_a["timezone_id"]} lang={bf_a["lang"]} accept_lang={bf_a["accept_lang"]}')
print(f'  账号B features: window={bf_b["window_size"]} tz={bf_b["timezone_id"]} lang={bf_b["lang"]} accept_lang={bf_b["accept_lang"]}')
assert bf_a != bf_b, 'browser_features 未差异化'
# 向后兼容
bf_legacy = get_random_browser_features()
assert bf_legacy['window_size'] == '1920,1080'
print('  PASS browser_features 账号差异化 + 向后兼容')

# --- 5. real_mouse 每账号独立目录 ---
print('\n--- real_mouse 每账号独立目录 ---')
solver_a = _get_shared_solver("2232648246")
solver_b = _get_shared_solver("test_acc_9")
print(f'  solver A dir: {solver_a.browser_dir}')
print(f'  solver B dir: {solver_b.browser_dir}')
assert solver_a.browser_dir != solver_b.browser_dir, 'real_mouse 目录未隔离!'
assert '2232648246' in solver_a.browser_dir
# pure_id 截断是既有行为(更新日志标识), 此处只验证两账号目录不同即可
assert solver_b.pure_id != solver_a.pure_id
# 复用同账号(同 pure_id)应返回同实例
solver_a2 = _get_shared_solver("2232648246_sub")
assert solver_a2 is solver_a, '同 pure_id 未复用 solver'
print('  PASS 每账号独立目录 + 同账号复用')

print()
print('=' * 70)
print('=== 阶段C 验证全部通过 ===')
print('=' * 70)
