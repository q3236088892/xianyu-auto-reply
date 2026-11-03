"""阶段D 验证: 代理工厂 scheme 翻译 + fail-closed + 关键文件导入完整性。

不断网、不起服务: 纯函数级断言 + import 验证。
"""
import os
os.environ['BROWSER_HEADLESS'] = 'true'

from common.services.proxy_factory import (
    ProxyConfigError,
    aiohttp_proxy_url,
    chrome_proxy_arg,
    httpx_proxy_url,
    playwright_proxy,
    require_proxy,
    resolve_proxy_fields,
    webrtc_policy_args,
)

print()
print('=' * 70)
print('阶段D 验证: 代理工厂与 fail-closed')
print('=' * 70)

# --- 1. scheme 翻译 ---
print('\n--- scheme 翻译(同一 socks5 在不同库语义不同) ---')
h = httpx_proxy_url('socks5', '1.2.3.4', 1080, 'u', 'p')
a = aiohttp_proxy_url('socks5', '1.2.3.4', 1080, 'u', 'p')
pw = playwright_proxy('socks5', '1.2.3.4', 1080, 'u', 'p')
c = chrome_proxy_arg('socks5', '1.2.3.4', 1080)
print(f'  httpx      : {h}')
print(f'  aiohttp    : {a}')
print(f'  playwright : {pw}')
print(f'  chrome arg : {c}')
assert h.startswith('socks5h://'), f'httpx SOCKS5 必须用 socks5h://(代理侧DNS), 实际 {h}'
assert a.startswith('socks5://'), f'aiohttp SOCKS5 用 socks5://(python-socks 默认rdns=True), 实际 {a}'
assert pw and pw['server'] == 'socks5://1.2.3.4:1080' and pw['username'] == 'u'
assert c == 'socks5://1.2.3.4:1080'
print('  PASS SOCKS5 scheme 按库正确翻译')

h2 = httpx_proxy_url('http', '1.2.3.4', 8080)
assert h2 == 'http://1.2.3.4:8080'
assert httpx_proxy_url('none', 'x', 1) == ''
assert aiohttp_proxy_url('', 'x', 1) == ''
assert playwright_proxy('none', 'x', 1) is None
print('  PASS http/none 边界')

# 特殊字符密码 URL 编码
h3 = httpx_proxy_url('socks5', 'h', 1, 'u@ser', 'p:ass')
assert 'u%40ser' in h3 and 'p%3Aass' in h3, f'特殊字符未编码: {h3}'
print('  PASS 认证特殊字符 URL 编码')

# --- 2. fail-closed 校验 ---
print('\n--- fail-closed (R1.7) ---')
try:
    require_proxy('socks5', '', 0, context='测试')
    raise SystemExit('FAIL: require_proxy 未拦截缺 host')
except ProxyConfigError as e:
    print(f'  缺host/port -> ProxyConfigError: {e}')

require_proxy('none', '', 0)          # 未配置代理不触发
require_proxy('http', 'h', 8080)      # 完整配置不触发
print('  PASS fail-closed 校验(已配置但缺字段 -> 抛错; 未配置 -> 放行)')

# --- 3. WebRTC 封堵 ---
print('\n--- WebRTC 封堵 (R1.8) ---')
args = webrtc_policy_args()
assert '--force-webrtc-ip-handling-policy=disable_non_proxied_udp' in args
print(f'  {args}')
print('  PASS WebRTC 策略参数')

# --- 4. 字段提取 ---
print('\n--- resolve_proxy_fields ---')
class FakeAccount:
    proxy_type = 'socks5'
    proxy_host = 'h'
    proxy_port = 1080
    proxy_user = 'u'
    proxy_pass = 'p'
fields = resolve_proxy_fields(FakeAccount())
assert fields == {'proxy_type': 'socks5', 'proxy_host': 'h', 'proxy_port': 1080,
                  'proxy_user': 'u', 'proxy_pass': 'p'}
class Bare: pass
assert resolve_proxy_fields(Bare()) == {'proxy_type': None, 'proxy_host': None,
                                        'proxy_port': None, 'proxy_user': None, 'proxy_pass': None}
print('  PASS 字段提取(含属性缺失容错)')

# --- 5. 关键文件导入完整性(改动后可 import) ---
print('\n--- 关键文件导入完整性 ---')
mods = [
    'common.services.proxy_factory',
    'common.services.fingerprint_provider',
    'common.services.captcha.browser_features',
    'common.services.xianyu_login.login_do',
    'common.services.xianyu_login.face_verification',
    'common.services.im_token_api',
    'common.services.xianyu_mtop',
    'common.services.cookie_renew_browser_service',
    'common.services.captcha.slider_stealth',
    'common.services.captcha.real_mouse_slider',
]
import importlib
for m in mods:
    importlib.import_module(m)
    print(f'  ✓ {m}')

# backend-web / websocket 专属(需在对应 APP_DIR 下, 此处仅验证 common 层)
print()
print('=' * 70)
print('=== 阶段D 验证全部通过 ===')
print('=' * 70)
