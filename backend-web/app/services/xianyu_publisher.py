"""
闲鱼商品自动发布器

功能：
1. 使用 Playwright 无头浏览器自动化操作闲鱼发布页面
2. 支持上传图片、填写标题描述、设置价格等
3. 支持浏览器复用（批量发布时减少启动次数）
4. 支持反检测（隐藏 webdriver 标识）

迁移自旧项目 utils/xianyu_publisher.py，适配新框架异步架构
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger
from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from common.utils.browser_utils import ensure_playwright_browser_path, get_chromium_executable_path
from common.services.publish_image_service import cleanup_temp_images, download_remote_image


class XianyuPublisher:
    """闲鱼商品发布器
    
    使用 Playwright 控制浏览器完成商品发布流程：
    1. 访问发布页面（需要 Cookie 已注入）
    2. 上传商品图片
    3. 填写标题和描述
    4. 等待分类自动识别，选择分类
    5. 输入价格
    6. 设置包邮
    7. 点击发布，等待跳转到商品详情页
    """

    def __init__(self, static_root: str | Path | None = None):
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.playwright = None
        self.is_initialized = False
        self.current_cookie: Optional[str] = None
        self.temp_image_paths: list[str] = []
        self.static_root = Path(static_root) if static_root else None

    async def _resolve_upload_image_path(self, image_path: str) -> str:
        if re.match(r"^https?://", image_path, re.IGNORECASE):
            file_path = await download_remote_image(image_path)
            self.temp_image_paths.append(file_path)
            return file_path

        if image_path.startswith("/static/") or image_path.startswith("static/"):
            relative_path = image_path.lstrip("/").replace("static/", "", 1)
            if self.static_root:
                return str(self.static_root / relative_path)
            return os.path.abspath(image_path.lstrip("/"))

        if image_path.startswith("/"):
            return image_path

        return os.path.abspath(image_path)

    def _cleanup_temp_images(self):
        if not self.temp_image_paths:
            return
        cleanup_temp_images(self.temp_image_paths)
        self.temp_image_paths = []

    async def initialize(self, headless: bool = True, force_reinit: bool = False):
        """初始化浏览器（增强反检测）
        
        Args:
            headless: 是否使用无头模式
            force_reinit: 是否强制重新初始化（即使已经初始化）
        """
        if self.is_initialized and not force_reinit:
            logger.info("✅ 浏览器已初始化，复用现有实例")
            return

        # Docker环境下强制无头模式（容器内无显示器，有头模式会报错）
        if not headless and os.environ.get("BROWSER_HEADLESS", "").lower() == "true":
            logger.info("检测到BROWSER_HEADLESS=true，强制使用无头模式")
            headless = True

        if self.is_initialized and force_reinit:
            await self.close_only_browser()

        ensure_playwright_browser_path()
        self.playwright = await async_playwright().start()

        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-web-security",
            "--disable-features=IsolateOrigins,site-per-process",
            "--window-size=1920,1080",
            "--start-maximized",
        ]

        chromium_path = get_chromium_executable_path()

        launch_kwargs = dict(
            headless=headless,
            args=browser_args,
            ignore_default_args=["--enable-automation"],
        )
        if chromium_path:
            launch_kwargs["executable_path"] = chromium_path

        self.browser = await self.playwright.chromium.launch(**launch_kwargs)

        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            permissions=["geolocation", "notifications"],
            java_script_enabled=True,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )

        self.page = await self.context.new_page()

        # 注入 JS 隐藏自动化标识
        await self.page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
            window.chrome = { runtime: {} };
        """)

        self.page.set_default_timeout(30000)
        self.page.set_default_navigation_timeout(60000)
        self.is_initialized = True

        logger.info("✅ 浏览器初始化成功（已启用反检测）")

    async def set_cookies(self, cookies_str: str):
        """向浏览器注入闲鱼登录 Cookie
        
        Args:
            cookies_str: Cookie 字符串，格式为 key=value; key2=value2
        """
        if not self.context:
            raise Exception("浏览器未初始化，请先调用 initialize()")

        cookie_list = []
        for item in cookies_str.split(";"):
            item = item.strip()
            if "=" in item:
                name, value = item.split("=", 1)
                for domain in [".goofish.com", ".taobao.com", ".alipay.com"]:
                    cookie_list.append({
                        "name": name.strip(),
                        "value": value.strip(),
                        "domain": domain,
                        "path": "/",
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "Lax",
                    })

        await self.context.add_cookies(cookie_list)
        self.current_cookie = cookies_str
        logger.info(f"✅ 已注入 {len(cookie_list)} 个 Cookie（覆盖多个域名）")

    async def reinitialize_page(self):
        """复用浏览器实例，只重新创建页面（用于批量发布场景）"""
        if not self.is_initialized or not self.context:
            raise Exception("浏览器未初始化")

        if self.page:
            await self.page.close()

        self.page = await self.context.new_page()
        self.page.set_default_timeout(30000)
        self.page.set_default_navigation_timeout(60000)
        logger.info("✅ 页面已重新创建（浏览器复用）")

    async def publish_item(
        self,
        item_data: dict,
        cookie_data: dict,
        reuse_browser: bool = False,
        should_close: bool = True,
    ) -> dict:
        """发布商品到闲鱼（按原项目完整流程迁回）"""
        result = {
            "success": False,
            "message": "",
            "item_url": None,
            "item_id": None,
            "screenshot": None,
        }

        try:
            logger.info("=" * 80)
            logger.info("📝 开始发布商品")
            logger.info("=" * 80)
            logger.info(f"商品信息: {item_data.get('description', '')[:50]}...")
            logger.info(f"浏览器复用模式: {reuse_browser}")

            headless = True
            logger.info("🖥️ 使用无头模式（浏览器不可见）")

            if reuse_browser and self.is_initialized and self.current_cookie == cookie_data["cookie"]:
                logger.info("🔄 复用现有浏览器，重新创建页面...")
                await self.reinitialize_page()
            else:
                await self.initialize(headless=headless)

            if not (reuse_browser and self.is_initialized and self.current_cookie == cookie_data["cookie"]):
                await self.set_cookies(cookie_data["cookie"])

            await self.set_cookies(cookie_data["cookie"])

            logger.info("\n[步骤1] 🌐 先访问闲鱼首页，触发Cookie初始化...")
            await self.page.goto("https://www.goofish.com", wait_until="networkidle", timeout=30000)
            await asyncio.sleep(1)

            logger.info("\n[步骤2] 🌐 访问登录页面...")
            await self.page.goto(
                "https://login.taobao.com/member/login.jhtml",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await asyncio.sleep(1)

            publish_url = "https://www.goofish.com/publish?spm=a21ybx.item.sidebar.1.297e3da6aDZAmV"
            logger.info(f"\n[步骤3] 🌐 访问发布页面: {publish_url}")
            await self.page.goto(publish_url, wait_until="networkidle", timeout=60000)
            await asyncio.sleep(3)

            current_url = self.page.url
            logger.info("✅ 页面已加载")
            logger.info(f"当前URL: {current_url}")

            if "login" in current_url or "auth" in current_url:
                raise Exception("Cookie已失效，页面跳转到登录页")

            page_title = await self.page.title()
            logger.info(f"页面标题: {page_title}")

            page_text = await self.page.evaluate("() => document.body.innerText")

            if "登录后可以" in page_text or ("立即登录" in page_text and "扫码登录" not in page_text):
                logger.error("Cookie已失效，页面内容包含未登录提示")
                raise Exception("Cookie已失效，页面显示未登录状态")

            if "添加首图" not in page_text and "宝贝图片" not in page_text:
                logger.info("⏳ 等待React渲染发布页面...")
                await asyncio.sleep(5)
                page_text = await self.page.evaluate("() => document.body.innerText")

                if "添加首图" not in page_text and "宝贝图片" not in page_text:
                    logger.error("页面可能不是发布页面，或者Cookie无效")
                    logger.error(f"页面内容: {page_text[:500]}")
                    raise Exception("页面可能不是发布页面，或者Cookie无效")

            screenshot = await self.page.screenshot(full_page=True)
            result["screenshot"] = base64.b64encode(screenshot).decode()

            logger.info("✅ 登录状态正常")
            logger.info("\n⏳ 等待React应用渲染表单元素...")
            try:
                initial_inputs = await self.page.query_selector_all("input")
                logger.info(f"   初始状态: 找到 {len(initial_inputs)} 个input元素")

                if len(initial_inputs) == 0:
                    logger.info("   React应用可能还在渲染，等待表单元素出现...")
                    await self.page.wait_for_selector("input, button, textarea", timeout=30000)
                    logger.info("   ✅ 表单元素已渲染")
                else:
                    logger.info("   ✅ 表单元素已存在")

                all_inputs = await self.page.query_selector_all("input")
                all_buttons = await self.page.query_selector_all("button")
                all_textareas = await self.page.query_selector_all("textarea")
                logger.info(
                    f"   验证结果: {len(all_inputs)} 个input, {len(all_buttons)} 个button, {len(all_textareas)} 个textarea"
                )

            except Exception as e:
                logger.warning(f"   方法1失败，尝试方法2: {e}")
                await self.page.wait_for_function(
                    """
                    () => {
                        const inputs = document.querySelectorAll('input, button, textarea');
                        return inputs.length > 0;
                    }
                    """,
                    timeout=30000,
                )
                logger.info("   ✅ 表单元素已渲染（通过JavaScript）")

                all_inputs = await self.page.query_selector_all("input")
                all_buttons = await self.page.query_selector_all("button")
                all_textareas = await self.page.query_selector_all("textarea")
                logger.info(
                    f"   验证结果: {len(all_inputs)} 个input, {len(all_buttons)} 个button, {len(all_textareas)} 个textarea"
                )

            logger.info("✅ React应用渲染完成")

            captcha_selectors = [".nc-container", "#nc_1_n1z", ".captcha-container"]
            has_captcha = False
            for selector in captcha_selectors:
                captcha = await self.page.query_selector(selector)
                if captcha:
                    logger.warning(f"\n⚠️ 检测到滑块验证: {selector}")
                    logger.warning("需要通过滑块验证才能继续")
                    has_captcha = True
                    break

            if has_captcha:
                logger.warning("提示：可以集成滑块验证工具来自动处理")

            logger.info("\n[步骤2] 📷 上传商品图片...")
            images = item_data.get("images", [])
            if not images:
                raise Exception("❌ 未提供商品图片，无法发布")

            logger.info(f"共有 {len(images)} 张图片需要上传")
            await self._upload_images(images)

            logger.info("\n[步骤3] ⏳ 等待图片上传和分类自动识别...")
            # 关键修复：图片较多（如 7 张）时上传耗时长、页面 DOM 渲染更晚，
            # 原来固定 sleep(5) 后就往下走，分类选择区往往还没出现，
            # 导致 _select_category 的短超时全部落空、分类没选上，最终点发布不跳转。
            # 这里改为轮询等待分类相关元素真正出现（最多 20 秒），确保页面就绪后再继续。
            category_ready = False
            deadline = asyncio.get_event_loop().time() + 20
            while asyncio.get_event_loop().time() < deadline:
                try:
                    category_elements = await self.page.query_selector_all('[class*="category"], [class*="分类"]')
                    if category_elements:
                        logger.info(f"✅ 检测到 {len(category_elements)} 个分类相关元素")
                        category_ready = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(1)
            if not category_ready:
                logger.warning("⚠️ 20 秒内未检测到分类相关元素，页面可能仍在渲染，继续尝试后续步骤")
            # 额外给页面一点稳定时间，让分类/表单渲染完成。
            await asyncio.sleep(2)

            logger.info("\n[步骤4] 📝 填写宝贝描述...")
            await self._fill_description(item_data)

            logger.info("\n[步骤5] ⏳ 等待分类自动变化...")
            await asyncio.sleep(3)

            await self._select_category()

            logger.info("\n[步骤7] ⏭️ 跳过商品规格...")
            await asyncio.sleep(1)

            await self._fill_price(item_data)

            logger.info("\n[步骤10] ⏭️ 跳过服务选择...")
            await asyncio.sleep(1)
            logger.info("\n[步骤11] ⏭️ 跳过服务选择...")
            await asyncio.sleep(1)
            logger.info("\n[步骤11] ⏭️ 跳过服务选择...")
            await asyncio.sleep(1)

            await self._set_free_shipping()

            await self._set_item_address(item_data)

            await self._click_publish_button(result)

            logger.info("\n" + "=" * 80)
            logger.info("📝 发布流程完成")
            logger.info("=" * 80)
            await asyncio.sleep(3)

        except Exception as e:
            error_msg = f"发布失败: {str(e)}"
            result["message"] = error_msg
            logger.error(f"❌ {error_msg}")
            logger.error(f"错误详情: {type(e).__name__}: {str(e)}")

            if self.page:
                try:
                    screenshot = await self.page.screenshot(full_page=True)
                    result["screenshot"] = base64.b64encode(screenshot).decode()
                except Exception:
                    pass

        finally:
            if should_close:
                await self.close()

        return result

    async def _upload_images(self, images: list):
        """上传商品图片列表（按原项目流程）"""
        logger.info(f"[步骤4] 📷 上传 {len(images)} 张商品图片...")

        # 网络监听：记录上传相关的 XHR 请求和响应，用于诊断哪张图的上传请求失败
        upload_requests = []  # list of dict: {url, method, status, timestamp}
        last_upload_idx = [0]  # 当前正在上传的图片序号

        async def on_request(request):
            url = request.url
            if any(kw in url for kw in ['upload', 'image', 'photo', 'pic', 'file', 'mtop']):
                if 'goofish' in url or 'alicdn' in url or 'taobao' in url or 'alibaba' in url:
                    upload_requests.append({
                        'idx': last_upload_idx[0],
                        'url': url[:200],
                        'method': request.method,
                        'ts': asyncio.get_event_loop().time(),
                    })
                    logger.info(f"🌐 [网络] 图片 {last_upload_idx[0]} 发起请求: {request.method} {url[:120]}")

        async def on_response(response):
            url = response.url
            if any(kw in url for kw in ['upload', 'image', 'photo', 'pic', 'file']):
                if 'goofish' in url or 'alicdn' in url or 'taobao' in url:
                    body_preview = ''
                    try:
                        if response.status >= 400 or 'upload' in url:
                            body = await response.text()
                            body_preview = body[:500]
                    except:
                        pass
                    logger.info(f"🌐 [网络] 响应 {response.status}: {url[:120]}")
                    if body_preview:
                        logger.info(f"🌐 [网络] 响应体: {body_preview}")

        if self.page:
            self.page.on('request', lambda req: asyncio.ensure_future(on_request(req)))
            self.page.on('response', lambda resp: asyncio.ensure_future(on_response(resp)))

        add_image_selectors = [
            'span:has-text("添加首图")',
            'div:has-text("添加首图")',
            '[class*="upload-item"]',
            'button:has-text("添加")',
            'span:has-text("添加图片")',
            'div:has-text("添加图片")',
            'button:has-text("上传")',
            'span:has-text("上传")',
            'div:has-text("上传图片")',
            '[class*="upload"]',
            '[class*="image-upload"]',
            '[class*="upload-trigger"]',
            '.upload-btn',
            '.add-image-btn',
            '.upload-image',
            'i[class*="upload"]',
            'i[class*="image"]',
            'button[class*="upload"]',
            'button[class*="add"]',
            'svg[class*="upload"]',
            'text=添加图片',
            'text=上传图片',
            'text=添加首图',
            'text=上传',
        ]

        add_image_button = None
        for selector in add_image_selectors:
            try:
                add_image_button = await self.page.wait_for_selector(selector, timeout=3000)
                if add_image_button:
                    logger.info(f"✅ 找到上传按钮: {selector}")
                    break
            except Exception:
                continue

        if not add_image_button:
            file_input_selectors = [
                'input[type="file"][accept*="image"]',
                'input[type="file"]',
                'input[accept*="image"]',
                'input.file-input',
                'input[name*="file"]',
                'input[name*="image"]',
            ]

            file_input = None
            for selector in file_input_selectors:
                try:
                    file_input = await self.page.query_selector(selector)
                    if file_input:
                        logger.info(f"✅ 找到文件输入框: {selector}")
                        break
                except Exception:
                    continue

            if not file_input:
                all_inputs = await self.page.query_selector_all("input")
                logger.warning(f"⚠️ 未找到明确的上传按钮，页面共有 {len(all_inputs)} 个input")

                for inp in all_inputs:
                    try:
                        inp_type = await inp.get_attribute("type")
                        inp_accept = await inp.get_attribute("accept")
                        inp_name = await inp.get_attribute("name")
                        if inp_type == "file" or (inp_accept and "image" in inp_accept):
                            logger.info(
                                f"✅ 找到可能的文件输入框: type={inp_type}, accept={inp_accept}, name={inp_name}"
                            )
                            add_image_button = None
                            break
                    except Exception:
                        continue

                if add_image_button is None and not file_input:
                    raise Exception("未找到'添加首图'按钮或文件输入框")

        # 精确统计"已上传预览图"数量的选择器组。
        # 说明：原实现用 img[class*="img"] / [class*="image"] img 这类过于宽泛的选择器，
        # 会把页面上的图标、占位图、示例图也算进去；再配合"绝对数量 >= i"的判定，
        # 会在图片还没真正上传完时就误判为"已完成"，导致后续提前点发布、
        # 页面未跳转而发布失败（图越多越容易触发）。这里改为按"上传前后的增量"判断。
        preview_selectors = [
            '.imgList img',
            '[class*="uploaded"] img',
            '[class*="image-item"] img',
            'img[src*="upload"]',
            'img[src*="temp"]',
            'img[class*="img"]',
        ]

        async def count_previews() -> int:
            """统计当前页面已上传的预览图数量（取各选择器命中的最大值）。"""
            best = 0
            for selector in preview_selectors:
                try:
                    imgs = await self.page.query_selector_all(selector)
                    if len(imgs) > best:
                        best = len(imgs)
                except Exception:
                    continue
            return best

        # 以"页面预览图的绝对数量"作为唯一事实来源。
        # 历史踩坑：
        #   1) 早期用 img[class*="img"] 等宽泛选择器 + "绝对数量 >= i" 判定，
        #      会把占位/示例图算进去而提前误判为已完成；
        #   2) 后来改成逐张累加 uploaded_count，两张图并发渲染时会漏计（7 张只数到 6）；
        #   3) 再改成"初始基准 base_count + 已提交数"作目标，又踩到另一个坑——
        #      页面上传前就有 1 张占位 img，它会被第一张真实图“替换”而非“新增”，
        #      于是目标整体偏移 +1，7 张全传完仍差 1，报“6/7”。
        # 结论：占位图会被替换，基准不可靠。真正稳定的事实是——
        #   每张图上传后，页面预览图的绝对数量应达到当前序号 i；
        #   全部完成后，绝对数量应等于图片总数。故这里直接用绝对数量，不做任何基准减法。
        # 注意：闲鱼发布页的 file input 是唯一的常驻元素（不会随上传张数增加），
        # 每次 set_input_files 复用同一个 input 即可触发上传。
        for i, image_path in enumerate(images, 1):
            try:
                last_upload_idx[0] = i  # 供网络监听回调标记当前图片序号
                file_path = await self._resolve_upload_image_path(str(image_path))

                logger.info(f"上传第 {i}/{len(images)} 张图片: {file_path}")

                if not os.path.exists(file_path):
                    logger.warning(f"⚠️ 图片文件不存在: {file_path}")
                    continue

                file_input = None
                file_input_selectors = [
                    'input[type="file"][accept*="image"]',
                    'input[type="file"]',
                    'input[accept*="image"]',
                ]

                for selector in file_input_selectors:
                    try:
                        file_input = await self.page.query_selector(selector)
                        if file_input:
                            logger.info(f"找到文件输入框: {selector}")
                            break
                    except Exception:
                        continue

                if not file_input:
                    logger.error(f"未找到文件输入框，跳过第 {i} 张图片")
                    continue

                logger.info(f"正在上传图片 {i}（直接写入文件输入框，避免弹出系统选择窗口）...")
                await file_input.set_input_files(file_path)
                logger.info(f"✅ 已选择图片 {i}")

                # 主动触发 input 的 change 事件，确保 React 组件能捕获到文件变化。
                # 前面几张图 Playwright 的 set_input_files 会自动触发，
                # 但最后一张（达到上限时）前端可能不再监听，需要手动 dispatch。
                await self.page.evaluate("""() => {
                    const input = document.querySelector('input[type="file"][accept*="image"]');
                    if (input) {
                        input.dispatchEvent(new Event('change', { bubbles: true }));
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                }""")
                logger.info(f"已触发 change/input 事件")

                # 轮询等待页面预览图的绝对数量达到当前序号 i，最多等 30 秒。
                # 用绝对目标 i（而非基准偏移或增量累加），避开占位图被替换、
                # 并发渲染漏计等所有历史坑。
                target = i
                deadline = asyncio.get_event_loop().time() + 30
                now_count = await count_previews()
                while now_count < target and asyncio.get_event_loop().time() < deadline:
                    await asyncio.sleep(1)
                    now_count = await count_previews()
                if now_count >= target:
                    logger.info(f"✅ 图片 {i} 上传成功（预览图达到 {now_count} 张，目标 {target}）")
                else:
                    logger.warning(
                        f"⚠️ 图片 {i} 在 30 秒内未确认上传成功（预览图 {now_count} 张，目标 {target}）"
                    )
                    # 诊断：dump 页面状态，帮助定位为何预览图没有增加
                    await self._diagnose_image_upload_state(i, file_path, now_count, target, upload_requests)

                if i < len(images):
                    await asyncio.sleep(2)
                    continue_selectors = [
                        'span:has-text("添加细节图")',
                        'div:has-text("添加细节图")',
                        'span:has-text("添加")',
                        'div:has-text("添加")',
                        'button:has-text("添加")',
                    ]

                    continue_button = None
                    for selector in continue_selectors:
                        try:
                            continue_button = await self.page.query_selector(selector)
                            if continue_button:
                                btn_text = await continue_button.inner_text()
                                if "添加" in btn_text or "上传" in btn_text:
                                    logger.info(f"找到继续上传按钮: {btn_text}")
                                    await continue_button.click()
                                    await asyncio.sleep(1)
                                    break
                        except Exception:
                            continue

                    if not continue_button:
                        logger.warning("未找到继续上传按钮，尝试查找通用添加按钮")
                        add_image_button = await self.page.query_selector('span:has-text("添加")')
                        if add_image_button:
                            await add_image_button.click()
                            await asyncio.sleep(1)

            except Exception as e:
                logger.warning(f"⚠️ 图片 {i} 上传失败: {e}")
                continue

        # 总数校验：以"页面预览图的绝对数量"为唯一事实来源，不做任何基准减法。
        # 全部图片上传完成后，页面预览图的绝对数量应达到图片总数；
        # 否则说明确有图片没传上去，直接中止发布，绝不带着缺图点发布按钮。
        total = len(images)
        final_count = await count_previews()
        if final_count <= 0:
            raise Exception("❌ 没有成功上传任何图片")
        if final_count < total:
            raise Exception(
                f"❌ 图片未全部上传成功（仅 {final_count}/{total} 张），已中止发布，请重试"
            )

        # 记录本次期望的图片数量，供 _click_publish_button 点击发布前做最终断言，
        # 作为"绝不带着缺图点发布"的最后一道防线。
        self._expected_image_count = total

        logger.info(f"✅ 共上传 {final_count}/{total} 张图片")

    async def _diagnose_image_upload_state(self, i: int, file_path: str, now_count: int, target: int, upload_requests: list = None):
        """图片上传超时后的诊断：dump 页面上传区域的状态，帮助定位为何预览图没有增加。"""
        try:
            logger.warning(f"🔍 [诊断] 第 {i} 张上传超时，开始 dump 页面状态...")

            # 0) 该图相关的网络请求
            if upload_requests:
                relevant = [r for r in upload_requests if r['idx'] == i]
                if relevant:
                    logger.warning(f"🔍 [诊断] 第 {i} 张共发起 {len(relevant)} 个网络请求:")
                    for r in relevant[-5:]:  # 最多打5条
                        logger.warning(f"🔍 [诊断]   {r['method']} {r['url']}")
                else:
                    logger.warning(f"🔍 [诊断] 第 {i} 张没有发起任何上传相关的网络请求！")

            # 1) 所有 file input 的状态
            all_inputs = await self.page.query_selector_all('input[type="file"]')
            logger.warning(f"🔍 [诊断] 页面共有 {len(all_inputs)} 个 file input")
            for idx, inp in enumerate(all_inputs):
                try:
                    accept = await inp.get_attribute("accept")
                    name = await inp.get_attribute("name")
                    visible = await inp.is_visible()
                    logger.warning(f"🔍 [诊断]   input[{idx}]: accept={accept}, name={name}, visible={visible}")
                except Exception as e:
                    logger.warning(f"🔍 [诊断]   input[{idx}]: 读取属性失败: {e}")

            # 2) "添加细节图" / "添加" 按钮是否还存在
            for selector in ['span:has-text("添加细节图")', 'div:has-text("添加细节图")',
                             'span:has-text("添加")', 'div:has-text("添加")']:
                btn = await self.page.query_selector(selector)
                if btn:
                    try:
                        txt = await btn.inner_text()
                        visible = await btn.is_visible()
                        logger.warning(f"🔍 [诊断] 找到按钮 [{txt}] visible={visible} (selector={selector})")
                    except:
                        pass

            # 3) 上传区域的 HTML 片段（前 3000 字符）
            try:
                html = await self.page.evaluate("""() => {
                    const upload = document.querySelector('[class*="upload"]')
                        || document.querySelector('[class*="imgList"]')
                        || document.querySelector('[class*="image"]');
                    return upload ? upload.outerHTML.substring(0, 3000) : '(未找到上传区域)';
                }""")
                logger.warning(f"🔍 [诊断] 上传区域 HTML 片段:\n{html}")
            except Exception as e:
                logger.warning(f"🔍 [诊断] 获取上传区域 HTML 失败: {e}")

            # 4) 页面 URL 和 title
            logger.warning(f"🔍 [诊断] 当前URL: {self.page.url}")
            logger.warning(f"🔍 [诊断] 页面标题: {await self.page.title()}")

        except Exception as e:
            logger.warning(f"🔍 [诊断] 诊断过程出错: {e}")

    async def _set_item_address(self, item_data: dict):
        address = (item_data.get("address") or "").strip()
        expected_text = (item_data.get("address_expected_text") or "").strip()
        if not address:
            raise Exception("未获取到可用的宝贝所在地")

        if expected_text:
            logger.info(f"\n[步骤13] 📍 设置宝贝所在地，搜索关键词: {address}，期望文本: {expected_text}")
        else:
            logger.info(f"\n[步骤13] 📍 设置宝贝所在地，搜索关键词: {address}")

        trigger_selectors = [
            'div:has-text("宝贝所在地")',
            'span:has-text("宝贝所在地")',
            'label:has-text("宝贝所在地")',
            '[class*="location"]',
            '[class*="address"]',
            '[class*="地区"]',
        ]
        modal_selectors = [
            '.ant-modal-content',
            '.ant-modal-wrap',
            '[role="dialog"]',
        ]

        modal = None
        for selector in trigger_selectors:
            try:
                triggers = await self.page.query_selector_all(selector)
            except Exception:
                continue

            for trigger in triggers:
                try:
                    if not await trigger.is_visible():
                        continue

                    trigger_text = re.sub(r"\s+", " ", (await trigger.inner_text()).strip())
                    if selector in {'[class*="location"]', '[class*="address"]', '[class*="地区"]'} and '宝贝所在地' not in trigger_text:
                        continue

                    await trigger.click()
                    await asyncio.sleep(1)

                    for modal_selector in modal_selectors:
                        try:
                            current_modal = await self.page.query_selector(modal_selector)
                            if current_modal and await current_modal.is_visible():
                                modal = current_modal
                                logger.info(f"✅ 已打开宝贝所在地弹窗: {selector}")
                                break
                        except Exception:
                            continue

                    if modal:
                        break
                except Exception:
                    continue

            if modal:
                break

        if not modal:
            raise Exception("未找到宝贝所在地设置入口")

        input_selectors = [
            'input[placeholder*="请输入"]',
            'input[placeholder*="选择"]',
            'input[placeholder*="地址"]',
            'input',
        ]

        search_input = None
        for selector in input_selectors:
            try:
                inputs = await modal.query_selector_all(selector)
            except Exception:
                continue

            for current_input in inputs:
                try:
                    if not await current_input.is_visible():
                        continue
                    box = await current_input.bounding_box()
                    if not box or box.get("height", 0) < 20:
                        continue
                    search_input = current_input
                    logger.info(f"✅ 找到宝贝所在地搜索框: {selector}")
                    break
                except Exception:
                    continue

            if search_input:
                break

        if not search_input:
            raise Exception("未找到宝贝所在地搜索框")

        await search_input.click()
        await asyncio.sleep(0.3)
        try:
            await search_input.fill("")
        except Exception:
            await search_input.press("Control+A")
            await search_input.press("Backspace")
        await asyncio.sleep(0.4)
        await search_input.type(address, delay=150)
        await asyncio.sleep(2.5)

        input_box = None
        try:
            input_box = await search_input.bounding_box()
        except Exception:
            input_box = None

        option_selectors = [
            '.ant-list-item',
            '.ant-select-item-option',
            '[role="option"]',
            '[class*="list"] [class*="item"]',
            '[class*="option"]',
            'li',
            'div',
            'span',
            'button',
            'a',
        ]

        valid_options = []
        seen_texts = set()
        exclude_texts = {"宝贝所在地", address, "确定", "确认", "取消"}

        amap_container = None
        try:
            amap_container = await self.page.query_selector('.amap-sug-result')
            if amap_container and await amap_container.is_visible():
                amap_option_selectors = [
                    '.amap-sug-result [class*="item"]',
                    '.amap-sug-result li',
                    '.amap-sug-result div',
                    '.amap-sug-result span',
                ]
                amap_valid_options = []
                for selector in amap_option_selectors:
                    try:
                        options = await self.page.query_selector_all(selector)
                    except Exception:
                        continue

                    current_valid_options = []
                    for option in options:
                        try:
                            if not await option.is_visible():
                                continue

                            option_text = re.sub(r"\s+", " ", (await option.inner_text()).strip())
                            lines = [line.strip() for line in re.split(r"[\r\n]+", option_text) if line.strip()]
                            normalized_text = " / ".join(lines)
                            if not normalized_text or normalized_text in seen_texts or normalized_text in exclude_texts:
                                continue
                            if any(text in normalized_text for text in ["选择精准地址", "帮你推给更多同城买家", "宝贝所在地", "搜索", "清空", "常用地址", "附近地址"]):
                                continue
                            if len(normalized_text) < 2 or len(normalized_text) > 120:
                                continue
                            if len(lines) > 4:
                                continue

                            box = await option.bounding_box()
                            if not box:
                                continue
                            if box.get("height", 0) < 18 or box.get("height", 0) > 180:
                                continue
                            if box.get("width", 0) < 40:
                                continue

                            current_valid_options.append((normalized_text, option))
                            seen_texts.add(normalized_text)
                        except Exception:
                            continue

                    if current_valid_options:
                        amap_valid_options = current_valid_options
                        break

                if amap_valid_options:
                    valid_options = amap_valid_options
                    option_texts = [text for text, _ in valid_options]
                    if len(option_texts) <= 30:
                        logger.info(f"✅ 在高德地址候选列表中搜索到 {len(valid_options)} 个宝贝所在地候选: {option_texts}")
                    else:
                        logger.info(f"✅ 在高德地址候选列表中搜索到 {len(valid_options)} 个宝贝所在地候选，前10项: {option_texts[:10]}")
        except Exception:
            amap_container = None

        search_roots = [
            ("弹窗", modal),
            ("页面", self.page),
        ]

        if not valid_options:
            for root_name, root in search_roots:
                for selector in option_selectors:
                    try:
                        options = await root.query_selector_all(selector)
                    except Exception:
                        continue

                    current_valid_options = []
                    for option in options:
                        try:
                            if not await option.is_visible():
                                continue

                            option_text = re.sub(r"\s+", " ", (await option.inner_text()).strip())
                            lines = [line.strip() for line in re.split(r"[\r\n]+", option_text) if line.strip()]
                            normalized_text = " / ".join(lines)
                            if not normalized_text or normalized_text in seen_texts or normalized_text in exclude_texts:
                                continue
                            if any(text in normalized_text for text in ["选择精准地址", "帮你推给更多同城买家", "宝贝所在地", "搜索", "清空", "常用地址", "附近地址"]):
                                continue
                            if len(normalized_text) < 2 or len(normalized_text) > 80:
                                continue
                            if len(lines) > 3:
                                continue

                            box = await option.bounding_box()
                            if not box:
                                continue
                            if box.get("height", 0) < 18 or box.get("height", 0) > 160:
                                continue
                            if box.get("width", 0) < 40:
                                continue
                            if input_box:
                                if box.get("y", 0) + box.get("height", 0) <= input_box.get("y", 0):
                                    continue
                                if box.get("y", 0) - input_box.get("y", 0) > 700:
                                    continue
                                if box.get("x", 0) + box.get("width", 0) < input_box.get("x", 0) - 120:
                                    continue
                                if box.get("x", 0) > input_box.get("x", 0) + input_box.get("width", 0) + 360:
                                    continue

                            current_valid_options.append((normalized_text, option))
                            seen_texts.add(normalized_text)
                        except Exception:
                            continue

                    if current_valid_options:
                        valid_options = current_valid_options
                        option_texts = [text for text, _ in valid_options]
                        if len(option_texts) <= 30:
                            logger.info(f"✅ 在{root_name}中搜索到 {len(valid_options)} 个宝贝所在地候选: {option_texts}")
                        else:
                            preview_texts = option_texts[:10]
                            logger.info(f"✅ 在{root_name}中搜索到 {len(valid_options)} 个宝贝所在地候选，前10项: {preview_texts}")
                        break

                if valid_options:
                    break

        if not valid_options:
            visible_texts = []
            try:
                nearby_elements = await self.page.query_selector_all('div, li, span, button, a')
                for element in nearby_elements:
                    try:
                        if not await element.is_visible():
                            continue
                        text = re.sub(r"\s+", " ", (await element.inner_text()).strip())
                        if not text or text in visible_texts:
                            continue
                        box = await element.bounding_box()
                        if not box or not input_box:
                            continue
                        if box.get("y", 0) + box.get("height", 0) <= input_box.get("y", 0):
                            continue
                        if box.get("y", 0) - input_box.get("y", 0) > 700:
                            continue
                        if len(text) > 50:
                            continue
                        visible_texts.append(text)
                        if len(visible_texts) >= 8:
                            break
                    except Exception:
                        continue
            except Exception:
                visible_texts = []
            if visible_texts:
                logger.warning(f"⚠️ 未匹配到候选，搜索框附近可见文本: {visible_texts}")
            raise Exception(f"未找到“{address}”对应的宝贝所在地候选")

        candidate_options = valid_options
        confirm_selectors = [
            '.ant-modal-footer button.ant-btn-primary',
            '.ant-modal-footer button',
            'button:has-text("确定")',
            'button:has-text("确认")',
            'button:has-text("完成")',
            '[role="button"]:has-text("确定")',
            '[role="button"]:has-text("确认")',
        ]

        def _normalize_search_keyword(value: str) -> str:
            return re.sub(r"\s+$", "", value or "")

        def _sort_candidate_options(options):
            if not expected_text:
                return options

            normalized_expected_text = _normalize_search_keyword(expected_text)

            def _candidate_sort_key(option_item):
                option_text = _normalize_search_keyword(option_item[0])
                if option_text == normalized_expected_text:
                    return (0, len(option_text), 0)
                if option_text.startswith(normalized_expected_text):
                    return (1, len(option_text), 0)
                if normalized_expected_text in option_text:
                    return (2, len(option_text), option_text.find(normalized_expected_text))
                return (3, len(option_text), 0)

            return sorted(options, key=_candidate_sort_key)

        candidate_options = _sort_candidate_options(candidate_options)

        async def _refresh_address_candidates(search_keyword: str, retry_count: int = 0):
            search_value = search_keyword + (" " * retry_count)
            await search_input.click()
            await asyncio.sleep(0.5)
            try:
                await search_input.fill("")
            except Exception:
                await search_input.press("Control+A")
                await search_input.press("Backspace")
            await asyncio.sleep(0.4)
            await search_input.type(search_value, delay=150)
            await asyncio.sleep(2.5)

        async def _collect_current_candidate_options(search_keyword: str):
            normalized_search_keyword = _normalize_search_keyword(search_keyword)
            current_options = []
            current_seen = set()

            async def _append_options(options, max_length: int, max_lines: int, check_input_box: bool):
                for option in options:
                    try:
                        if not await option.is_visible():
                            continue

                        option_text = re.sub(r"\s+", " ", (await option.inner_text()).strip())
                        lines = [line.strip() for line in re.split(r"[\r\n]+", option_text) if line.strip()]
                        normalized_text = " / ".join(lines)
                        if not normalized_text or normalized_text in current_seen or normalized_text in exclude_texts:
                            continue
                        if any(text in normalized_text for text in ["选择精准地址", "帮你推给更多同城买家", "宝贝所在地", "搜索", "清空", "常用地址", "附近地址"]):
                            continue
                        if _normalize_search_keyword(normalized_text) == normalized_search_keyword:
                            continue
                        if len(normalized_text) < 2 or len(normalized_text) > max_length:
                            continue
                        if len(lines) > max_lines:
                            continue

                        box = await option.bounding_box()
                        if not box:
                            continue
                        if box.get("height", 0) < 18:
                            continue
                        if box.get("width", 0) < 40:
                            continue
                        if check_input_box and input_box:
                            if box.get("y", 0) + box.get("height", 0) <= input_box.get("y", 0):
                                continue
                            if box.get("y", 0) - input_box.get("y", 0) > 700:
                                continue
                            if box.get("x", 0) + box.get("width", 0) < input_box.get("x", 0) - 120:
                                continue
                            if box.get("x", 0) > input_box.get("x", 0) + input_box.get("width", 0) + 360:
                                continue

                        current_options.append((normalized_text, option))
                        current_seen.add(normalized_text)
                    except Exception:
                        continue

            try:
                amap_result = await self.page.query_selector('.amap-sug-result')
                if amap_result and await amap_result.is_visible():
                    amap_option_selectors = [
                        '.amap-sug-result [class*="item"]',
                        '.amap-sug-result li',
                        '.amap-sug-result div',
                        '.amap-sug-result span',
                    ]
                    for selector in amap_option_selectors:
                        try:
                            options = await self.page.query_selector_all(selector)
                        except Exception:
                            continue
                        await _append_options(options, 120, 4, False)
                        if current_options:
                            return current_options
            except Exception:
                pass

            for _, root in search_roots:
                for selector in option_selectors:
                    try:
                        options = await root.query_selector_all(selector)
                    except Exception:
                        continue
                    await _append_options(options, 80, 3, True)
                    if current_options:
                        return current_options

            return current_options

        async def _try_confirm_current_selection() -> bool:
            confirmed = False
            for selector in confirm_selectors:
                try:
                    confirm_button = await modal.query_selector(selector)
                    if confirm_button and await confirm_button.is_visible() and await confirm_button.is_enabled():
                        await confirm_button.click()
                        await asyncio.sleep(1)
                        logger.info("✅ 已确认宝贝所在地")
                        confirmed = True
                        break
                except Exception:
                    continue

            modal_visible = False
            try:
                modal_visible = await modal.is_visible()
            except Exception:
                modal_visible = False

            if not modal_visible:
                if confirmed:
                    logger.info("✅ 已确认宝贝所在地")
                else:
                    logger.info("✅ 宝贝所在地弹窗已关闭")
                return True

            return False

        for selected_index, (selected_text, initial_option) in enumerate(candidate_options, 1):
            try:
                selected_option = initial_option

                logger.info(f"🎯 尝试选择宝贝所在地[{selected_index}/{len(candidate_options)}]: {selected_text}")
                await selected_option.click()
                await asyncio.sleep(1)

                if await _try_confirm_current_selection():
                    return

                logger.warning(f"⚠️ 当前候选选择后弹窗仍未关闭，继续尝试下一个: {selected_text}")

                retry_query = _normalize_search_keyword(selected_text)
                await _refresh_address_candidates(retry_query, selected_index - 1)
                retry_options = _sort_candidate_options(await _collect_current_candidate_options(retry_query))
                if not retry_options:
                    logger.warning(f"⚠️ 使用候选词重试后未找到可用候选，尝试下一个原始候选: {selected_text}")
                    continue

                for retry_index, (retry_text, retry_option) in enumerate(retry_options, 1):
                    try:
                        logger.info(f"↪️ 使用候选词重试[{retry_index}/{len(retry_options)}]: {retry_text}")
                        await retry_option.click()
                        await asyncio.sleep(1)
                        if await _try_confirm_current_selection():
                            return
                        logger.warning(f"⚠️ 候选词重试后弹窗仍未关闭，继续尝试下一个: {retry_text}")
                    except Exception as retry_error:
                        logger.warning(f"⚠️ 候选词重试失败，继续下一个: {retry_text}, 错误: {retry_error}")
            except Exception as e:
                logger.warning(f"⚠️ 尝试宝贝所在地候选失败，继续下一个: {selected_text}, 错误: {e}")

        raise Exception(f"未找到可关闭宝贝所在地弹窗的“{address}”候选")

    # 闲鱼发布页会校验"商品描述不能包含emoji"，命中即静默拦截提交。
    # 在写入富文本编辑器前先把这些符号清掉，避免页面停留、按钮无响应。
    # 范围覆盖：emoji 表情、各类装饰符号、麻将/扑克牌字符、封闭字母/表意补充，
    # 以及常见的 ✓ / ✔ / ✅ / ✗ / ❎ / ☑ / ⭐ / ✨ / ➕ / ➖ / … 等。
    _EMOJI_PATTERN = re.compile(
        "["
        "\U0001F300-\U0001FAFF"
        "\U0001F000-\U0001F1FF"
        "\U0001F200-\U0001F2FF"
        "\U0001F600-\U0001F64F"
        "\U0001F900-\U0001F9FF"
        "\U0001FA70-\U0001FAFF"
        "⌀-⏿"
        "☀-⛿"
        "✀-➿"
        "⬀-⭿"
        "︀-️"
        "‍"
        "]+",
        flags=re.UNICODE,
    )

    @classmethod
    def _strip_emojis(cls, text: str) -> str:
        """移除 emoji / 装饰符号，避免触发闲鱼"商品描述不能包含emoji"校验。"""
        if not text:
            return text
        cleaned = cls._EMOJI_PATTERN.sub("", text)
        # 去掉 emoji 后把残留的多余空白/全角空格折叠成单个半角空格
        cleaned = re.sub(r"[ \t　]+", " ", cleaned)
        return cleaned

    async def _fill_description(self, item_data: dict):
        """填写宝贝描述（按原项目流程）"""
        desc_selectors = [
            'div[data-placeholder*="描述一下宝贝的品牌型号"]',
            'div[data-placeholder*="描述"]',
            'div[contenteditable="true"]',
            '.editor',
            '[class*="editor"]',
        ]

        desc_input = None
        for selector in desc_selectors:
            try:
                desc_input = await self.page.wait_for_selector(selector, timeout=5000)
                if desc_input:
                    is_contenteditable = await desc_input.evaluate('el => el.getAttribute("contenteditable")')
                    if is_contenteditable == "true" or selector.startswith('div[data-placeholder'):
                        logger.info(f"✅ 找到描述输入框: {selector}")
                        break
            except Exception:
                continue

        if not desc_input:
            raise Exception("未找到描述输入框")

        title = item_data.get("title", "")
        description = item_data.get("description", "")

        if title and description:
            full_description = f"{title}\n\n{description}"
        elif title:
            full_description = title
        else:
            full_description = description

        # 写入富文本前剥离 emoji/装饰符号，避免触发闲鱼"商品描述不能包含emoji"拦截
        full_description = self._strip_emojis(full_description)
        logger.info(f"描述内容（去emoji后）: {full_description[:100]}...")
        await desc_input.click()
        await asyncio.sleep(0.5)
        await desc_input.evaluate(f"el => el.innerText = {json.dumps(full_description, ensure_ascii=False)}")

        logger.info("✅ 描述已填写")

    def _get_category_text_lines(self, text: str) -> list[str]:
        return [item.strip() for item in re.split(r"[\r\n]+", text or "") if item.strip()]

    def _split_category_text(self, text: str) -> list[str]:
        parts = []
        for value in self._get_category_text_lines(text):
            if value in {"分类", "*"}:
                continue
            parts.append(value)
        return parts

    def _normalize_category_text(self, text: str) -> str:
        parts = []
        for item in self._split_category_text(text):
            if item not in parts:
                parts.append(item)
        return " / ".join(parts)

    async def _get_current_category_text(self, category_selectors: list[str]) -> str:
        for selector in category_selectors:
            try:
                category_element = await self.page.query_selector(selector)
                if not category_element:
                    continue
                return self._normalize_category_text(await category_element.inner_text())
            except Exception:
                continue
        return ""

    async def _get_leaf_category_options(self, container=None, exclude_texts: set[str] | None = None):
        root = container or self.page
        if not root:
            return []

        option_selectors = [
            '.ant-select-item-option',
            '.ant-select-item',
            '[class*="ant-select-item"]',
            '[role="option"]',
            'li',
            'div[class*="option"]',
            'div[class*="item"]',
            'span[class*="item"]',
            '.category-option',
            '.category-item',
            '[class*="category-item"]',
            '[class*="CategoryItem"]',
        ]
        excluded = {
            normalized
            for normalized in [self._normalize_category_text(text) for text in (exclude_texts or set())]
            if normalized
        }

        async def collect_options(selectors: list[str]):
            best_collected = []
            for selector in selectors:
                try:
                    options = await root.query_selector_all(selector)
                except Exception:
                    continue

                current_options = []
                current_seen = set()
                for option in options:
                    try:
                        if not await option.is_visible():
                            continue

                        raw_text = await option.inner_text()
                        raw_lines = self._get_category_text_lines(raw_text)
                        if len(raw_lines) != 1:
                            continue

                        text = self._normalize_category_text(raw_text)
                        if not text or text in excluded or text in current_seen:
                            continue

                        box = await option.bounding_box()
                        if not box:
                            continue
                        if box.get("height", 0) <= 0 or box.get("width", 0) <= 0:
                            continue
                        if box.get("height", 0) > 52:
                            continue

                        current_seen.add(text)
                        current_options.append((text, option, box))
                    except Exception:
                        continue

                current_options.sort(
                    key=lambda item: (
                        round(item[2].get("y", 0), 2),
                        round(item[2].get("x", 0), 2),
                        round(item[2].get("height", 0), 2),
                        round(item[2].get("width", 0), 2),
                    )
                )
                normalized_options = [(text, option) for text, option, _ in current_options]
                if len(normalized_options) > len(best_collected):
                    best_collected = normalized_options

            return best_collected

        best_options = await collect_options(option_selectors)
        if len(best_options) > 1:
            return best_options

        fallback_options = await collect_options(['div', 'span', 'button', 'a'])
        if len(fallback_options) > len(best_options):
            logger.info(f"分类候选通用扫描补充找到 {len(fallback_options)} 个候选")
            return fallback_options

        if len(best_options) <= 1:
            logger.warning(f"分类候选提取不足，仅找到 {len(best_options)} 个候选")

        return best_options

    async def _reopen_category_candidates(
        self,
        category_selectors: list[str],
        category_list_selectors: list[str],
        exclude_texts: set[str] | None = None,
    ):
        category_element = None
        current_selected_text = ""
        for selector in category_selectors:
            try:
                category_element = await self.page.query_selector(selector)
                if category_element:
                    try:
                        current_selected_text = self._normalize_category_text(await category_element.inner_text())
                    except Exception:
                        current_selected_text = ""
                    break
            except Exception:
                continue

        if not category_element:
            return []

        await category_element.click()
        await asyncio.sleep(2)

        category_list = None
        for selector in category_list_selectors:
            try:
                category_list = await self.page.query_selector(selector)
                if category_list:
                    break
            except Exception:
                continue

        if not category_list:
            return []

        all_excluded = set(exclude_texts or set())
        if current_selected_text:
            all_excluded.add(current_selected_text)
        return await self._get_leaf_category_options(category_list, exclude_texts=all_excluded)

    async def _select_category(self):
        """选择商品分类（按原项目整体逻辑迁回）"""
        logger.info("\n[步骤6] 📂 选择分类...")

        category_selectors = [
            '[class*="category"]:has(button)',
            '[class*="分类"]:has(button)',
            '.category-select',
            'button:has-text("分类")',
            '.category-item',
        ]

        category_element = None
        for selector in category_selectors:
            try:
                category_element = await self.page.query_selector(selector)
                if category_element:
                    logger.info(f"✅ 找到分类元素: {selector}")
                    break
            except Exception:
                continue

        if category_element:
            await category_element.click()
            await asyncio.sleep(1)
            await asyncio.sleep(1)

            option_selectors = [
                '[class*="option"]',
                '[class*="dropdown-item"]',
                'li:visible',
                '.category-option',
            ]

            options = None
            for selector in option_selectors:
                try:
                    options = await self.page.query_selector_all(selector)
                    if options and len(options) > 0:
                        logger.info(f"✅ 找到 {len(options)} 个分类选项")
                        break
                except Exception:
                    continue

            if options:
                first_option = options[0]
                option_text = await first_option.inner_text()
                logger.info(f"选择分类: {option_text}")
                await first_option.click()
                await asyncio.sleep(1)

                logger.info("\n[步骤7] 🔍 检查分类是否可用...")

                restricted_text = "网页版暂不支持发布此分类"
                restricted_selector = f"text={restricted_text}"

                try:
                    restricted = await self.page.wait_for_selector(restricted_selector, timeout=2000)
                    if restricted:
                        logger.error(f"❌ 检测到限制: {restricted_text}")
                        logger.error("❌ 此分类无法在网页版发布，请更换分类")
                        raise Exception(f"此分类无法在网页版发布: {option_text}")
                except Exception:
                    logger.info("✅ 分类可用，可以继续发布")

                logger.info("\n[步骤7.1] 🔍 检查是否需要选择子分类...")
                await asyncio.sleep(2)

                max_category_levels = 5
                for level in range(max_category_levels):
                    try:
                        new_category = await self.page.query_selector('[class*="category"]:visible, [class*="分类"]:visible')
                        if not new_category:
                            break

                        await new_category.click()
                        await asyncio.sleep(1)

                        options = await self.page.query_selector_all('[class*="option"]:visible, li:visible')
                        if options:
                            option_text = await options[0].inner_text()
                            logger.info(f"选择第 {level + 2} 级分类: {option_text}")
                            await options[0].click()
                            await asyncio.sleep(1)

                            try:
                                restricted = await self.page.wait_for_selector(restricted_selector, timeout=1000)
                                if restricted:
                                    logger.error(f"❌ 检测到限制: {restricted_text}")
                                    raise Exception(f"此分类无法在网页版发布: {option_text}")
                            except Exception:
                                pass
                        else:
                            break
                    except Exception:
                        break

                logger.info("✅ 分类选择完成")
            else:
                logger.warning("⚠️ 未找到分类选项，可能分类已自动选择")
        else:
            logger.warning("⚠️ 未找到分类选择元素，跳过")

        logger.info("\n[步骤6] 📂 选择固定分类...")

        category_selectors = [
            '[class*="categoryText"]',
            '[class*="category"]',
            'div:has-text("属性规格")',
            '[class*="categoryText--MCLwjrBN"]',
            'div[class*="Category"]',
            'span[class*="category"]',
            '[class*="Category"]',
            'div:has-text("选择分类")',
            'div:has-text("分类")',
            'span:has-text("选择分类")',
            '.next-select',
            '.ant-select',
            '[role="combobox"]',
            'input[placeholder*="分类"]',
            'input[placeholder*="类目"]',
        ]

        category_element = None
        for selector in category_selectors:
            try:
                category_element = await self.page.wait_for_selector(selector, timeout=2000)
                if category_element:
                    logger.info(f"✅ 找到分类元素: {selector}")
                    break
            except Exception:
                continue

        if category_element:
            await category_element.click()
            await asyncio.sleep(2)

            logger.info("查找分类选项...")

            category_list_selectors = [
                'div.ant-select-dropdown',
                '.ant-select-dropdown',
                '[class*="categoryList"]',
                '[class*="category-list"]',
                '[role="listbox"]',
                '.ant-dropdown',
            ]

            category_list = None
            for selector in category_list_selectors:
                try:
                    category_list = await self.page.wait_for_selector(selector, timeout=2000)
                    if category_list:
                        logger.info(f"✅ 找到分类列表: {selector}")
                        break
                except Exception:
                    continue

            if category_list:
                category_options = await self._get_leaf_category_options(category_list)

                if category_options:
                    logger.info(f"找到 {len(category_options)} 个分类选项")

                    category_text, first_category = category_options[0]
                    logger.info(f"选择分类: {category_text}")
                    await first_category.click()
                    await asyncio.sleep(2)

                    restricted_text = "网页版暂不支持发布此分类"
                    restricted_selector = f"text={restricted_text}"

                    try:
                        restricted = await self.page.wait_for_selector(restricted_selector, timeout=2000)
                        if restricted:
                            logger.warning(f"⚠️ 检测到限制: {restricted_text}")
                            logger.info("当前分类不支持发布，尝试选择其他分类...")

                            retry_options = await self._reopen_category_candidates(
                                category_selectors=category_selectors,
                                category_list_selectors=category_list_selectors,
                                exclude_texts={category_text},
                            )
                            if retry_options:
                                second_category_text, second_category = retry_options[0]
                                logger.info(f"选择第二个分类: {second_category_text}")
                                await second_category.click()
                                await asyncio.sleep(2)

                                try:
                                    restricted = await self.page.wait_for_selector(restricted_selector, timeout=2000)
                                    if restricted:
                                        logger.warning("⚠️ 第二个分类也不支持，尝试第三个分类")
                                        third_retry_options = await self._reopen_category_candidates(
                                            category_selectors=category_selectors,
                                            category_list_selectors=category_list_selectors,
                                            exclude_texts={category_text, second_category_text},
                                        )
                                        if third_retry_options:
                                            third_category_text, third_category = third_retry_options[0]
                                            logger.info(f"选择第三个分类: {third_category_text}")
                                            await third_category.click()
                                            await asyncio.sleep(2)
                                        else:
                                            logger.warning("⚠️ 请手动选择分类后发布")
                                except Exception:
                                    logger.info("✅ 第二个分类可用")
                            else:
                                logger.error("❌ 没有其他分类可选，跳过分类选择")
                    except Exception:
                        logger.info("✅ 分类可用，继续发布")

                    await asyncio.sleep(2)
                    logger.info("检查是否需要选择子分类...")

                    for level in range(3):
                        try:
                            sub_category_list = await self.page.query_selector('[class*="categoryList"]:visible, [class*="ant-dropdown"]:visible')

                            if not sub_category_list:
                                break

                            sub_options = await sub_category_list.query_selector_all('[role="option"]:visible, li:visible')

                            if sub_options:
                                sub_text = await sub_options[0].inner_text()
                                logger.info(f"选择第 {level + 2} 级分类: {sub_text}")
                                await sub_options[0].click()
                                await asyncio.sleep(2)

                                try:
                                    restricted = await self.page.wait_for_selector(restricted_selector, timeout=1000)
                                    if restricted:
                                        logger.warning("⚠️ 子分类受限，停止选择")
                                        break
                                except Exception:
                                    pass
                            else:
                                break
                        except Exception:
                            break

                    logger.info("✅ 分类选择完成")
                else:
                    logger.warning("⚠️ 未找到分类选项，跳过")
            else:
                logger.warning("⚠️ 未找到分类列表，跳过")
        else:
            logger.warning("⚠️ 未找到分类选择元素，跳过")

    async def _fill_price(self, item_data: dict):
        """填写售价和原价（按原项目流程）"""
        logger.info("\n[步骤8] 💰 输入价格...")

        price = item_data.get("price", 0)
        logger.info(f"价格: {price}")

        price_selectors = [
            'input[placeholder*="价格"]',
            'input[placeholder*="售价"]',
            'input[placeholder*="多少钱"]',
            '.price input',
            '[class*="price"] input',
        ]

        price_input = None
        for selector in price_selectors:
            try:
                price_input = await self.page.wait_for_selector(selector, timeout=3000)
                if price_input:
                    logger.info(f"✅ 找到价格输入框: {selector}")
                    break
            except Exception:
                continue

        if price_input:
            await price_input.fill(str(price))
            logger.info("✅ 价格已输入")
        else:
            logger.warning("⚠️ 未找到价格输入框")

        logger.info("\n[步骤9] 💰 输入原价（可选）...")

        original_price = item_data.get("original_price", 0)
        if original_price and float(original_price) > 0:
            logger.info(f"原价: {original_price}")

            original_price_selectors = [
                'input[placeholder*="原价"]',
                'input[placeholder*="划线价"]',
                '.original-price input',
                '[class*="original-price"] input',
            ]

            original_price_input = None
            for selector in original_price_selectors:
                try:
                    original_price_input = await self.page.wait_for_selector(selector, timeout=3000)
                    if original_price_input:
                        logger.info(f"✅ 找到原价输入框: {selector}")
                        break
                except Exception:
                    continue

            if original_price_input:
                await original_price_input.fill(str(original_price))
                logger.info("✅ 原价已输入")
            else:
                logger.info("ℹ️ 未找到原价输入框，跳过（原价是可选的）")
        else:
            logger.info("ℹ️ 未设置原价，跳过")

    async def _set_free_shipping(self):
        """设置发货方式为包邮（按原项目流程）"""
        logger.info("\n[步骤12] 🚚 设置发货方式为包邮...")

        free_shipping_selectors = [
            'button:has-text("包邮")',
            'div:has-text("包邮")',
            '[class*="free-shipping"]',
            '[class*="包邮"]',
        ]

        free_shipping_btn = None
        for selector in free_shipping_selectors:
            try:
                free_shipping_btn = await self.page.query_selector(selector)
                if free_shipping_btn:
                    logger.info(f"✅ 找到包邮按钮: {selector}")
                    break
            except Exception:
                continue

        if free_shipping_btn:
            await free_shipping_btn.click()
            logger.info("✅ 已选择包邮")
        else:
            logger.warning("⚠️ 未找到包邮按钮")

    async def _click_publish_button(self, result: dict):
        """点击发布按钮并等待发布结果（按原项目流程）"""
        logger.info("\n[步骤14] 🎯 点击发布按钮...")

        not_supported_warning = await self.page.query_selector('text=网页版暂不支持发布此分类')
        if not_supported_warning:
            logger.warning("⚠️ 检测到不支持的分类提示")
            logger.warning("尝试选择其他分类...")

            category_selectors = [
                '[class*="categoryText"]',
                '[class*="category"]',
                'div:has-text("选择分类")',
                'div:has-text("分类")',
                '[role="combobox"]',
            ]
            category_list_selectors = [
                'div.ant-select-dropdown',
                '.ant-select-dropdown',
                '[class*="categoryList"]',
                '[class*="category-list"]',
                '[role="listbox"]',
                '.ant-dropdown',
            ]
            first_category_text = await self._get_current_category_text(category_selectors)
            retry_options = await self._reopen_category_candidates(
                category_selectors=category_selectors,
                category_list_selectors=category_list_selectors,
            )

            if retry_options:
                second_category_text, second_category = retry_options[0]
                logger.info(f"选择第二个分类: {second_category_text}")
                await second_category.click()
                await asyncio.sleep(2)

                not_supported_warning = await self.page.query_selector('text=网页版暂不支持发布此分类')
                if not_supported_warning:
                    logger.warning("⚠️ 第二个分类也不支持，尝试第三个分类")
                    third_retry_options = await self._reopen_category_candidates(
                        category_selectors=category_selectors,
                        category_list_selectors=category_list_selectors,
                        exclude_texts={text for text in [first_category_text, second_category_text] if text},
                    )
                    if third_retry_options:
                        third_category_text, third_category = third_retry_options[0]
                        logger.info(f"选择第三个分类: {third_category_text}")
                        await third_category.click()
                        await asyncio.sleep(2)
                        not_supported_warning = await self.page.query_selector('text=网页版暂不支持发布此分类')
                        if not_supported_warning:
                            logger.error("❌ 前三个分类都不支持，继续尝试发布")
                    else:
                        logger.error("❌ 只有一个分类选项且不支持，继续尝试发布")
                else:
                    logger.info("✅ 第二个分类可用")
            else:
                logger.error("❌ 未找到可重选的分类项，继续尝试发布")

        publish_selectors = [
            '.publish-button--KBpTVopQ',
            'button.publish-button--KBpTVopQ',
            'button:has-text("发布")',
            'button:has-text("立即发布")',
            'button.publish-btn',
            '.publish-btn button',
            'button[type="submit"]',
        ]

        publish_btn = None
        publish_btn_selector = None
        for selector in publish_selectors:
            try:
                publish_btn = await self.page.wait_for_selector(selector, timeout=5000)
                if publish_btn:
                    if await publish_btn.is_visible() and await publish_btn.is_enabled():
                        publish_btn_selector = selector
                        logger.info(f"✅ 找到发布按钮: {selector}")
                        break
            except Exception:
                continue

        if publish_btn:
            await self.page.screenshot(full_page=True)

            await asyncio.sleep(2)

            # 点击发布前的最终防线：确认页面上的预览图数量达到期望值。
            # _upload_images 已把期望张数记到 self._expected_image_count；
            # 万一上游流程有变化、图片在点击前被移除或未渲染完成，这里直接拦下，
            # 避免"图不全就点发布"导致页面不跳转的失败。
            expected_image_count = getattr(self, "_expected_image_count", 0)
            if expected_image_count:
                preview_selectors = [
                    '.imgList img',
                    '[class*="uploaded"] img',
                    '[class*="image-item"] img',
                    'img[src*="upload"]',
                    'img[src*="temp"]',
                    'img[class*="img"]',
                ]
                current_previews = 0
                for selector in preview_selectors:
                    try:
                        imgs = await self.page.query_selector_all(selector)
                        if len(imgs) > current_previews:
                            current_previews = len(imgs)
                    except Exception:
                        continue
                if current_previews < expected_image_count:
                    raise Exception(
                        f"❌ 发布前校验失败：页面预览图仅 {current_previews}/{expected_image_count} 张，"
                        "图片未就绪，已中止发布，请重试"
                    )
                logger.info(f"✅ 发布前校验通过：预览图 {current_previews}/{expected_image_count} 张")

            logger.info("🚀 点击发布按钮...")
            publish_target = self.page.locator(publish_btn_selector).first if publish_btn_selector else None
            if publish_target is None:
                raise Exception("未找到可用的发布按钮定位器")
            await publish_target.click(timeout=5000)

            logger.info("\n[步骤15] ⏳ 等待发布完成...")
            logger.info("等待5秒，让发布请求处理...")
            await asyncio.sleep(5)

            logger.info("检查页面是否跳转...")
            await asyncio.sleep(3)

            screenshot_after = await self.page.screenshot(full_page=True)
            result["screenshot"] = base64.b64encode(screenshot_after).decode()

            current_url = self.page.url
            logger.info(f"当前页面URL: {current_url}")

            page_text = await self.page.evaluate('() => document.body.innerText')

            is_item_page = '/item/' in current_url or 'id=' in current_url

            if is_item_page:
                result["success"] = True
                result["message"] = '商品发布成功（已跳转到商品详情页）'
                result["item_url"] = current_url

                item_id_match = re.search(r'id=(\d+)', current_url)
                if item_id_match:
                    result["item_id"] = item_id_match.group(1)

                logger.info("✅✅✅ 商品发布成功！")
                logger.info("✅ 已跳转到商品详情页")
                logger.info(f"✅ 商品链接: {current_url}")

                has_manage_buttons = '下架' in page_text and '删除' in page_text
                if has_manage_buttons:
                    logger.info("✅ 已找到下架和删除按钮，确认发布成功")
                    result["success_flag"] = 'detected_manage_buttons'
                else:
                    logger.info("⚠️ 未找到下架和删除按钮，但URL已跳转到商品详情页")
                    result["success_flag"] = 'url_jumped'

            elif '发布成功' in page_text or '已发布' in page_text:
                result["success"] = True
                result["message"] = '商品发布成功（页面显示成功提示）'
                logger.info("✅✅✅ 商品发布成功（页面显示成功提示）")

            elif '成功' in page_text and ('发布' in page_text or '上架' in page_text):
                result["success"] = True
                result["message"] = '商品发布成功（检测到成功提示）'
                logger.info("✅✅✅ 商品发布成功（检测到成功提示）")

            elif '发布失败' in page_text or '失败' in page_text or '错误' in page_text:
                result["success"] = False
                result["message"] = '商品发布失败，页面显示错误提示'
                result["failure_reason"] = 'error_message_detected'
                logger.error("❌ 商品发布失败，页面显示错误提示")

            elif '发闲置' in current_url or 'publish' in current_url:
                result["success"] = False
                result["failure_reason"] = 'page_not_redirected'
                logger.warning("⚠️ 可能发布失败")
                logger.warning("⚠️ 页面未跳转，仍停留在发布页")
                logger.warning(f"⚠️ 当前URL: {current_url}")
                logger.warning("⚠️ 可能原因：宝贝所在地未设置、内容触发审核、账号风控等")
                # 诊断增强：未跳转时把页面完整文本打印出来、截图存盘，
                # 便于判断到底是"请选择分类"字段校验、风控验证还是审核拦截，
                # 避免靠猜测反复试错。
                hints = await self._dump_publish_failure_context(page_text)
                base_msg = '可能发布失败（页面未跳转，仍停留在发布页）'
                if hints:
                    result["message"] = f"{base_msg}；命中关键词：{', '.join(hints)}"
                else:
                    result["message"] = base_msg

            else:
                result["success"] = False
                result["failure_reason"] = 'unknown'
                logger.warning("⚠️ 无法确认发布状态")
                logger.warning(f"⚠️ 当前URL: {current_url}")
                logger.warning(f"⚠️ 页面文本: {page_text[:200]}")
                logger.warning("⚠️ 可能原因：宝贝所在地未设置、内容触发审核、账号风控等")
                hints = await self._dump_publish_failure_context(page_text)
                base_msg = '无法确认发布状态'
                if hints:
                    result["message"] = f"{base_msg}；命中关键词：{', '.join(hints)}"
                else:
                    result["message"] = base_msg

        else:
            result["message"] = '未找到发布按钮'
            logger.error("❌ 未找到发布按钮")

    async def _dump_publish_failure_context(self, page_text: str) -> list[str]:
        """发布未跳转时，转储页面文本与整页截图，供事后定位真实拦截原因。

        - 把页面完整可见文本打到日志（截断到 2000 字，避免刷屏）；
        - 命中一批常见关键词时明确点名（请选择分类 / 验证 / 审核 / 风控等）；
        - 把整页截图存到 logs/app/publish_failures/ 下，文件名带时间戳。
        这样"页面未跳转"不再只是猜测，而有据可查。

        返回命中到的关键词列表，方便调用方把它挂到 result["message"] 上回传前端，
        避免前端只看到笼统的"页面未跳转"而无从下手。
        """
        hit: list[str] = []
        try:
            logger.warning("—— 发布失败页面文本（截断 2000 字）——")
            logger.warning((page_text or "")[:2000])
            logger.warning("—— 页面文本结束 ——")

            # 命中常见拦截关键词时直接点名，便于一眼定位。
            hints = [
                "请选择分类", "请选择", "请填写", "必填", "不能为空",
                "验证", "滑动", "拖动", "安全", "校验",
                "审核", "风控", "违规", "限制", "频繁", "异常",
                "商品描述不能包含emoji", "请输入宝贝的品牌",
            ]
            hit = [kw for kw in hints if kw and kw in (page_text or "")]
            if hit:
                logger.warning(f"⚠️ 页面命中疑似拦截关键词: {hit}")

            # 整页截图存盘。
            from pathlib import Path
            from datetime import datetime

            dump_dir = Path("logs") / "app" / "publish_failures"
            dump_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            shot_path = dump_dir / f"publish-fail-{stamp}.png"
            await self.page.screenshot(path=str(shot_path), full_page=True)
            logger.warning(f"⚠️ 发布失败整页截图已保存: {shot_path.resolve()}")
        except Exception as exc:
            logger.warning(f"转储发布失败上下文时出错（忽略）: {exc}")
        return hit

    async def close_only_browser(self):
        """只关闭浏览器，不停止playwright"""
        try:
            if self.page:
                await self.page.close()
                self.page = None
            if self.context:
                await self.context.close()
                self.context = None
            if self.browser:
                await self.browser.close()
                self.browser = None
            logger.info("✅ 浏览器已关闭（保持playwright运行）")
        except Exception as e:
            logger.error(f"关闭浏览器时出错: {e}")
        finally:
            self._cleanup_temp_images()

    async def close(self):
        """关闭浏览器和playwright"""
        try:
            if self.page:
                await self.page.close()
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
            self.is_initialized = False
            self.current_cookie = None
            logger.info("✅ 浏览器和playwright已关闭")
        except Exception as e:
            logger.error(f"关闭浏览器时出错: {e}")
        finally:
            self._cleanup_temp_images()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()
