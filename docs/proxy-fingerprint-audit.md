# 闲鱼出站代理 + 指纹一致性核查清单

> **用法**：每次核查以此为基线逐项打勾，**禁止只抽查**。每项必须附证据（命令输出/日志行/commit）。
> 新代码只允许做两件事：① 对清单外新增的闲鱼触达点追加条目；② 对已勾条目重新核验。
> 全部勾完并在文末签署「完成核查」+ 日期 + commit + 证据摘要，才算一轮核查结束。

- 基线建立：2026-11-05，commit `4ca335a`
- 账号级代理唯一来源：`xy_accounts.proxy_*`（改配置前端「账号管理→代理设置」）
- 公共助手：`common/services/proxy_factory.py`（`proxy_connector_for_account` / `account_aiohttp_proxy_url` / `build_aiohttp_proxy_connector` / `playwright_proxy` / `require_proxy` / `httpx_proxy_url`）
- SOCKS5 认证中继：`common/services/socks5_relay.py`（async 中继 + `SyncSocks5Relay` + `prepare_playwright_proxy_sync`）
- 统一 UA 来源：`browser_user_agent()` / `browser_sec_ch_ua()`（`common/utils/xianyu_utils.py`，取本机 Chrome 主版本）
- 指纹档案：`get_account_browser_features(account_id)`（`common/services/captcha/browser_features.py`，阶段C）
- 原则：**fail-closed**——配了代理的账号，代理/配置出错一律抛错，禁止静默降级直连

---

## A. 登录链路

| # | 位置 | 出站 | 代理 | 指纹 | 状态 |
|---|---|---|---|---|---|
| A1 | `backend-web/app/services/qr_login/manager.py` | 扫码登录 HTTP | ✅ `session.proxy`(httpx socks5h) | httpx 头=统一UA | ☐ |
| A2 | `backend-web/app/services/password_login/flow.py` + `common/services/xianyu_login/login_do.py` + `face_verification.py` | 密码登录全流程 | ✅ 共享 AsyncClient 带 `proxy_url` | 统一UA | ☐ |
| A3 | `backend-web/app/services/captcha/xianyu_slider_stealth.py` | 登录滑块浏览器 | ✅ 继承 slider_stealth(见C1) | ✅ 指纹档案 browser_features | ☐ |

## B. 聊天链路（两套 IM 都要查）

| # | 位置 | 出站 | 代理 | 指纹 | 状态 |
|---|---|---|---|---|---|
| B1 | `websocket/app/services/xianyu/xianyu_async.py`（HTTP session/Token刷新/心跳） | mtop API | ✅ `_build_session_connector` ProxyConnector | 统一UA | ☐ |
| B2 | `websocket/app/services/xianyu/connection_manager.py` | 账号 WS 长连接 | ✅ python-socks `proxy.connect`；**失败抛 ConnectionError 重试，禁止直连** | WS 头 | ☐ |
| B3 | `websocket/app/services/xianyu/cookie_token_manager.py` | IM Token API + 图片上传 | ✅ request_im_token 带 connector；ImageUploader 带 connector | 统一UA | ☐ |
| B4 | `backend-web/app/services/chat_new/im_client.py` | 第二套 IM（wss-goofish.dingtalk.com）+ Token API | ✅ session/ws_connect 带 connector | 统一UA（含 DingTalk 复合UA动态版本） | ☐ |
| B5 | `common/utils/image_uploader.py` ×2（common+websocket副本） | 聊天图片上传 stream-upload.goofish.com | ✅ proxy_connector 参数 | — | ☐ |
| B6 | `websocket/app/services/xianyu/xianyu_async.py:_get_image_size_from_url` | 图片 CDN 拉取 | ✅ connector | — | ☐ |

## C. 浏览器启动点（每个都必须：代理 + WebRTC封堵 + 指纹档案）

| # | 位置 | 代理 | WebRTC | 指纹档案 | 状态 |
|---|---|---|---|---|---|
| C1 | `common/services/captcha/slider_stealth.py` | ✅ + socks5认证走 SyncSocks5Relay | ✅ | ✅ get_account_browser_features | ☐ |
| C2 | `common/services/captcha/real_mouse_slider.py` | ✅ 同上 | ✅ | ✅（channel=chrome） | ☐ |
| C3 | `common/services/captcha/drissionpage_slider.py` | ✅ co.set_proxy + 中继 | ✅ | profile 目录按账号 | ☐ |
| C4 | `backend-web/app/services/xianyu_publisher.py`（发布浏览器） | ✅ initialize(proxy=) + 中继 | ✅ | ✅ browser_features 透传(4ca335a起) | ☐ |
| C5 | `common/services/cookie_renew_browser_service.py`（Cookie续期浏览器） | ✅ + 中继 | ✅ | UA统一 | ☐ |
| C6 | `websocket/app/services/xianyu/cookies_refresh_service.py`（WS服务Cookies续期浏览器） | ✅ + 中继 | ✅ | UA统一 | ☐ |
| C7 | `backend-web/app/services/search/browser.py`（匿名搜索浏览器） | ❌ 不接（无账号上下文，非账号行为，白名单见D） | ❌ | UA统一 | ☐ |

## D. HTTP/API 直连点（已逐个接线，grep 复核方法见核验命令）

| # | 位置 | 出站 | 状态 |
|---|---|---|---|
| D1 | `common/services/xianyu_mtop.py` mtop_call | 通用 mtop | ✅ proxy 参数，调用方必传（见D2-D6） | ☐ |
| D2 | `common/services/xianyu_order_client.py` + 调用方 auto_order_task/listing_monitor_task | 下单 | ✅ account级/task级 proxy | ☐ |
| D3 | `common/services/xianyu_detail_client.py` + seller_fill_task | 商品详情 | ✅（修复了丢弃proxy的bug） | ☐ |
| D4 | `common/services/item_service.py` + `common/utils/item_info_manager.py` | 商品列表同步 | ✅ proxy_fields 参数 | ☐ |
| D5 | `websocket/app/services/xianyu/auto_delivery_handler.py`（订单详情/评价数/关闭订单 3处） | 发货辅助 mtop | ✅ `_get_xianyu_connector()` | ☐ |
| D6 | `websocket/app/services/shipping/{confirm,freeshipping}_service.py` | 确认发货/免拼 | ✅ 复用 xianyu_async 已代理 session | ☐ |
| D7 | `common/services/order_service.py` ×4（已售/退款/详情/原始详情） | 订单同步 | ✅ 代理优先+共享connector兜底 | ☐ |
| D8 | `common/services/item_delete_service.py` / `item_offline_service.py` | 删除/下架 | ✅ connector | ☐ |
| D9 | `common/services/rate_service.py` ×2 + `websocket/.../rate_service.py` | 评价 | ✅ connector（用户API内容源属第三方，不走代理） | ☐ |
| D10 | `common/services/im_token_api.py`（+2调用方） | IM Token | ✅ proxy_connector 参数 | ☐ |
| D11 | `common/utils/fish_nick_utils.py` | 买家昵称 | ✅ connector | ☐ |
| D12 | `websocket/.../buyer_credit_rule.py` | 买家信用 | ✅ connector | ☐ |
| D13 | `common/services/cookie_renew_api_service.py` ×3 | Cookie续期API | ✅ connector（account_id贯通4层） | ☐ |
| D14 | `common/services/captcha/token_refetch.py` | 滑块Token刷新 | ✅ httpx sync + 代理URL | ☐ |
| D15 | `common/utils/xianyu_utils.py:close_account_notice` | 关闭账号通知 | ✅ connector | ☐ |
| D16 | `backend-web/app/services/chat_new/avatar_service.py` / `official_blacklist_service.py` | 头像/官方黑名单 | ✅ connector | ☐ |
| D17 | `backend-web/app/services/data_analysis_service.py` ×2 + 路由 | 数据分析 | ✅ connector | ☐ |
| D18 | `scheduler/.../login_renew_task.py` / `polish_task.py` / `red_flower_task.py` | 登录续期/擦亮/小红花 | ✅ connector | ☐ |
| D19 | `common/services/fetch_proxy_from_api`（监控任务级代理API） | 代理API | ✅ fail-closed 抛 ConnectionError | ☐ |

## E. 指纹一致性专项

| # | 检查项 | 方法 | 状态 |
|---|---|---|---|
| E1 | 全库无硬编码 UA 版本字面量 | `grep -rn "Chrome/1[0-9][0-9]" --include=*.py` 排除白名单后应为 0 | ☐ |
| E2 | 全库无硬编码 sec-ch-ua 版本 | 同上，命中应为 0 | ☐ |
| E3 | 所有带 Cookie 的请求头 UA=browser_user_agent() | 抽 D 组各一项看 headers 构造 | ☐ |
| E4 | 发布浏览器视口/locale/时区=账号指纹档案 | initialize 消费 browser_features；档案值可用 get_account_browser_features 查 | ☐ |
| E5 | 滑块/发布/续期浏览器 UA 同源（同 major 版本） | browser_user_agent() 全局唯一来源 | ☐ |
| E6 | WebRTC 封堵参数覆盖所有浏览器启动点 | `--force-webrtc-ip-handling-policy=disable_non_proxied_udp` × C1-C6 | ☐ |

## F. 白名单（确认不改，防止误报）

- 服务间 `127.0.0.1` 内部委托（scheduler→websocket / cookie_renew→websocket / 后端静态等）
- 用户配置的第三方 API：卡券 API、易帆 API、评价内容 API、监控任务代理API本身（API URL 是供应商域名）
- 匿名商品搜索浏览器 C7（无账号身份）
- `staging/`、`releases/`、前端构建产物

## G. 核验命令（每轮照跑并贴输出）

```bash
cd wt-acct-proxy
# G1 残余直连扫描（输出应只剩白名单项）
for f in $(grep -rln "goofish.com\|m.goofish\|mtop\." common backend-web websocket scheduler promotion --include=*.py | grep -v __pycache__); do grep -n "ClientSession()" "$f"; done
# G2 硬编码 UA 扫描（输出应为空）
grep -rn "Chrome/1[0-9][0-9]" common backend-web websocket scheduler --include=*.py | grep -vE "browser_user_agent|sec_ch_ua|browser_chrome|Chrome 版本|BLBeacon"
# G3 降级关键词扫描（输出应为空，命中=存在直连退路）
grep -rn "回退直连\|降级直连\|不使用代理\|本机直连: \|本次直连" common backend-web websocket scheduler --include=*.py | grep -v "禁止\|fail-closed\|不降级"
# G4 运行时实证：聊天走代理
grep "代理连接建立成功" releases/<active>/app/websocket/logs/websocket.log | tail -3
# G5 发布链路实证：中继+代理+指纹
grep -E "中继已启动|发布浏览器走代理" releases/<active>/app/backend-web/logs/backend-web.log | tail -5
```

---

## 核查记录

### 2026-11-05 commit 4ca335a —— ✅ 完成核查
- **代理覆盖**：G1 残余 4 处裸 ClientSession 逐个人工确认（2 个本机内部、1 个第三方卡券API、0 个账号直连）→ 新接 D15（关闭通知）、B6（图片尺寸）后 G1 账号类清零；D1-D19 逐项接线于 17d89f0，commit message 有全清单
- **fail-closed**：G3 命中 0（9 处降级点已于 6271f1d 封死）
- **指纹**：G2 命中 0（28 处硬编码 UA + 5 处 sec-ch-ua 已于 4ca335a 统一为 browser_user_agent()/browser_sec_ch_ua()，实测输出 Chrome/156）；E4 发布浏览器接档案（384670656 档案 1536x864 实测）
- **运行时证据**：G4「代理连接建立成功→connected」00:43:21；代理断开时「连接中止(不降级直连)」00:42:43（fail-closed 实证）
- **遗留说明**：C7 匿名搜索浏览器不接代理（无账号上下文，白名单）；手机 gost 链路（11080）为外部依赖，fail-closed 下挂了会断线重试不会直连
