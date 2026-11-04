"""
公共商品发布执行服务

功能：
1. 统一执行单品发布的业务编排
2. 在发布前处理地址解析和发布日志
3. 在发布成功后自动同步账号商品
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.xy_account import XYAccount
from common.services.item_service import ItemService
from common.services.proxy_factory import playwright_proxy, require_proxy
from common.services.publish_address_service import PublishAddressService
from common.services.publish_log_service import PublishLogService
from common.services.xianyu_publish_service import publish_single_item


async def _get_account(session: AsyncSession, account_id: str, user_id: int) -> Optional[XYAccount]:
    """获取用户可用的闲鱼账号。"""
    stmt = (
        select(XYAccount)
        .where(
            XYAccount.account_id == account_id,
            XYAccount.owner_id == user_id,
        )
        .order_by(desc(XYAccount.id))
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalars().first()


def _build_account_proxy(account: Optional[XYAccount], account_id: str) -> Optional[Dict[str, Any]]:
    """基于账号的 proxy_* 字段构建 Playwright launch 的 proxy dict。

    fail-closed：已配置代理但缺 host/port 时抛 ProxyConfigError，禁止降级直连
    （否则登录IP与发布行为IP不一致，易触发风控）。
    """
    if account is None:
        return None
    ptype = account.proxy_type or "none"
    require_proxy(ptype, account.proxy_host, account.proxy_port, context=f"发布账号 {account_id}")
    return playwright_proxy(
        ptype,
        account.proxy_host or "",
        int(account.proxy_port or 0),
        account.proxy_user,
        account.proxy_pass,
    )


async def _sync_account_items_after_publish(
    session: AsyncSession,
    account_id: str,
    account: XYAccount,
) -> Dict[str, Any]:
    """发布成功后自动同步账号商品。"""
    item_svc = ItemService(session)
    try:
        sync_result = await item_svc.fetch_all_items_from_account(account=account)
        sync_status = "success" if sync_result.get("success") else "failed"
        sync_total_count = int(sync_result.get("total_count") or 0)
        sync_saved_count = int(sync_result.get("saved_count") or 0)
        if sync_status == "success":
            sync_message = f"已自动获取 {sync_total_count} 个商品，入库 {sync_saved_count} 个商品"
            logger.info(
                f"账号 {account_id} 发布后自动获取商品完成：共 {sync_total_count} 件，保存 {sync_saved_count} 件"
            )
        else:
            sync_message = f"自动获取商品失败：{sync_result.get('message') or '未知错误'}"
            logger.warning(
                f"账号 {account_id} 发布后自动获取商品失败，不影响后续发布：{sync_result.get('message', '未知错误')}"
            )
        return {
            "sync_status": sync_status,
            "sync_message": sync_message,
            "sync_total_count": sync_total_count,
            "sync_saved_count": sync_saved_count,
        }
    except Exception as sync_exc:
        logger.warning(
            f"账号 {account_id} 发布后自动获取商品异常，不影响后续发布：{sync_exc}"
        )
        return {
            "sync_status": "failed",
            "sync_message": f"自动获取商品异常：{sync_exc}",
            "sync_total_count": 0,
            "sync_saved_count": 0,
        }


async def execute_single_publish(
    session: AsyncSession,
    user_id: int,
    account_id: str,
    item_data: dict,
    static_root: str | Path | None = None,
) -> Dict[str, Any]:
    """执行单品发布并返回统一结果。"""
    log_svc = PublishLogService(session)
    address_svc = PublishAddressService(session)

    account = await _get_account(session=session, account_id=account_id, user_id=user_id)
    cookies_str = account.cookie if account and account.cookie else ""
    if not cookies_str:
        log = await log_svc.create_log(
            user_id=user_id,
            account_id=account_id,
            title=item_data.get("title", ""),
            description=item_data.get("description", ""),
            price=str(item_data.get("price", "")),
            material_id=item_data.get("id"),
            status="failed",
            error_message="账号不存在或无权使用",
        )
        return {"success": False, "message": "账号不存在或无权使用", "log_id": log.id}

    try:
        resolved_address = await address_svc.resolve_publish_address(account_id, item_data)
    except ValueError as exc:
        log = await log_svc.create_log(
            user_id=user_id,
            account_id=account_id,
            title=item_data.get("title", ""),
            description=item_data.get("description", ""),
            price=str(item_data.get("price", "")),
            material_id=item_data.get("id"),
            status="failed",
            error_message=str(exc),
        )
        return {"success": False, "message": str(exc), "log_id": log.id}

    publish_item_data = resolved_address.apply_to_item_data(item_data)
    log = await log_svc.create_log(
        user_id=user_id,
        account_id=account_id,
        title=item_data.get("title", ""),
        description=item_data.get("description", ""),
        price=str(item_data.get("price", "")),
        material_id=item_data.get("id"),
        status="publishing",
        **resolved_address.to_log_fields(),
    )

    result = None
    pub_error = None

    from common.db.session import async_session_maker

    async def _notify_auth_wait(qr_url: str) -> None:
        """发布风控认证弹窗出现/刷新时，把二维码链接实时写进发布日志，
        让用户在前端发布记录里直接看到并扫码，不用翻服务器日志找图。"""
        msg = "⏳ 等待扫码认证：闲鱼风控要求「完成认证才可继续发布」"
        if qr_url:
            msg += f"，请打开 {qr_url} 用闲鱼App扫码"
        msg += "（系统最长等待10分钟）"
        try:
            async with async_session_maker() as notify_session:
                await PublishLogService(notify_session).update_log(
                    log_id=log.id,
                    status="publishing",
                    error_message=msg,
                )
        except Exception as notify_exc:
            logger.error(f"更新认证等待状态失败: {notify_exc}")

    proxy = _build_account_proxy(account, account_id)
    # 阶段C指纹一致性：发布浏览器视口/locale/时区与滑块登录浏览器同一档案
    browser_features = None
    if account is not None:
        from common.services.captcha.browser_features import get_account_browser_features
        browser_features = get_account_browser_features(account.account_id)
    relay = None
    if proxy and proxy.get("username") and proxy.get("server", "").startswith("socks5://"):
        # Chromium 不支持带认证的 SOCKS5：起本地免密中继转发到带认证上游
        from common.services.socks5_relay import start_socks5_auth_relay
        relay = await start_socks5_auth_relay(
            proxy["server"], proxy["username"], proxy.get("password", ""),
        )
        proxy = relay.proxy_dict
    try:
        result = await publish_single_item(
            item_data=publish_item_data,
            cookie=cookies_str,
            static_root=static_root,
            on_auth_wait=_notify_auth_wait,
            proxy=proxy,
            browser_features=browser_features,
        )
    except Exception as exc:
        pub_error = exc
        logger.error(f"单品发布异常: {exc}")
    finally:
        if relay is not None:
            await relay.close()

    try:
        async with async_session_maker() as fresh_session:
            fresh_log_svc = PublishLogService(fresh_session)
            if pub_error:
                await fresh_log_svc.update_log(
                    log_id=log.id,
                    status="failed",
                    error_message=str(pub_error),
                )
                return {"success": False, "message": f"发布异常: {str(pub_error)}", "log_id": log.id}

            status = "success" if result.get("success") else "failed"
            await fresh_log_svc.update_log(
                log_id=log.id,
                status=status,
                item_url=result.get("item_url"),
                item_id=result.get("item_id"),
                error_message=None if result.get("success") else result.get("message"),
            )
    except Exception as db_err:
        logger.error(f"更新发布日志失败: {db_err}")

    if pub_error:
        return {"success": False, "message": f"发布异常: {str(pub_error)}", "log_id": log.id}

    publish_success = result.get("success", False)
    sync_info = {
        "sync_status": "skipped",
        "sync_message": "发布未成功，未触发自动获取商品",
        "sync_total_count": 0,
        "sync_saved_count": 0,
    }
    if publish_success and account is not None:
        sync_info = await _sync_account_items_after_publish(
            session=session,
            account_id=account_id,
            account=account,
        )

    message = result.get("message") or ("商品发布成功" if publish_success else "发布失败")
    if publish_success and sync_info.get("sync_message"):
        message = f"{message}，{sync_info['sync_message']}"

    return {
        "success": publish_success,
        "message": message,
        "item_url": result.get("item_url"),
        "item_id": result.get("item_id"),
        "log_id": log.id,
        **sync_info,
    }
