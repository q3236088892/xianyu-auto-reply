"""
二维码扫码登录路由

提供二维码生成、状态查询和Cookie获取接口
"""
from __future__ import annotations

import asyncio
from typing import Dict

from fastapi import APIRouter, Depends
from loguru import logger
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.http_client import get_http_client
from app.services.account_service import AccountService
from app.services.qr_login import qr_login_manager
from app.api.deps import get_db_session as get_db
from common.models.user import User
from common.schemas.common import ApiResponse
from common.services.account_limit_service import AccountLimitExceededError
from common.services.proxy_factory import httpx_proxy_url

router = APIRouter(prefix="/qr-login", tags=["二维码登录"])

# 会话元数据：owner_id + 可选的占位记录ID（扫码前创建、扫码成功后转正/过期删除）
SESSION_META: Dict[str, Dict] = {}

# 已处理的会话记录，防止重复处理
PROCESSED_SESSIONS: Dict[str, Dict] = {}

# 会话处理锁，防止并发处理同一个会话
SESSION_LOCKS: Dict[str, asyncio.Lock] = {}


class GenerateQRRequest(BaseModel):
    """生成二维码请求。带代理字段时会先创建一条占位账号记录：
    代理配置写入该记录，且二维码生成/轮询全程走该代理出口。
    扫码成功后占位记录转正（account_id 替换为真实ID，代理保留）。"""
    proxy_type: str | None = None
    proxy_host: str | None = None
    proxy_port: int | None = None
    proxy_user: str | None = None
    proxy_pass: str | None = None


def _get_session_lock(session_id: str) -> asyncio.Lock:
    """获取会话锁"""
    if session_id not in SESSION_LOCKS:
        SESSION_LOCKS[session_id] = asyncio.Lock()
    return SESSION_LOCKS[session_id]


def _cleanup_session(session_id: str):
    """清理会话相关数据"""
    SESSION_META.pop(session_id, None)
    SESSION_LOCKS.pop(session_id, None)


def _build_processed_response(processed_info: dict) -> ApiResponse:
    processed_status = processed_info.get("status")
    processed_message = processed_info.get("message") or (
        "扫码登录失败" if processed_status == "failed" else "扫码登录已完成"
    )
    return ApiResponse(
        success=True,
        message=processed_message,
        data={
            "status": "failed" if processed_status == "failed" else "already_processed",
            "message": processed_message,
            "account_info": {
                "account_id": processed_info.get("account_id"),
                "is_new_account": bool(processed_info.get("is_new_account", False)),
            },
        },
    )


@router.post("/generate")
async def generate_qr_code(
    payload: GenerateQRRequest | None = None,
    current_user: User = Depends(deps.get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    """
    生成二维码

    传入代理字段时：先创建占位账号记录（随机ID、代理已写入），
    且二维码生成请求全程经该代理出口，扫码成功后占位记录转正。
    返回二维码图片的Base64编码和会话ID。
    """
    pending_account_id: str | None = None
    account_service = AccountService(db)
    try:
        proxy_url: str | None = None
        has_proxy = bool(
            payload
            and payload.proxy_type
            and payload.proxy_type != "none"
            and payload.proxy_host
            and payload.proxy_port
        )
        if has_proxy:
            # 1. 建占位记录：代理配置先于 Cookie 签发落库
            pending = await account_service.create_pending_account(
                current_user.id,
                proxy_type=payload.proxy_type,
                proxy_host=payload.proxy_host,
                proxy_port=int(payload.proxy_port),
                proxy_user=payload.proxy_user or None,
                proxy_pass=payload.proxy_pass or None,
            )
            pending_account_id = pending.account_id
            # 2. 翻译成 httpx 代理 URL（SOCKS5 强制 socks5h，代理侧解析DNS）
            proxy_url = httpx_proxy_url(
                payload.proxy_type,
                payload.proxy_host,
                int(payload.proxy_port),
                payload.proxy_user,
                payload.proxy_pass,
            )

        result = await qr_login_manager.generate_qr_code(proxy=proxy_url)
        session_id = result.get("session_id")

        if result.get("success") and session_id:
            SESSION_META[session_id] = {
                "owner_id": current_user.id,
                "pending_account_id": pending_account_id,
            }
            logger.info(
                f"二维码生成成功: session_id={session_id}, user_id={current_user.id}, "
                f"pending_account_id={pending_account_id}"
            )
            return ApiResponse(
                success=True,
                message="二维码生成成功",
                data={
                    "session_id": session_id,
                    "qr_code_url": result.get("qr_code_url"),
                    "pending_account_id": pending_account_id,
                }
            )
        else:
            error_msg = result.get("message", "生成二维码失败")
            logger.error(f"二维码生成失败: {error_msg}")
            # 二维码生成失败：回滚删除占位记录，不留空壳
            if pending_account_id:
                await account_service.delete_pending_account(current_user.id, pending_account_id)
            return ApiResponse(
                success=False,
                message=error_msg,
            )
    except Exception as e:
        logger.exception("生成二维码时发生异常")
        if pending_account_id:
            try:
                await account_service.delete_pending_account(current_user.id, pending_account_id)
            except Exception:
                logger.exception(f"回滚删除占位记录失败: {pending_account_id}")
        return ApiResponse(
            success=False,
            message=f"生成二维码失败: {str(e)}",
        )


@router.get("/status/{session_id}")
async def get_qr_status(
    session_id: str,
    current_user: User = Depends(deps.get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    """
    查询扫码状态
    
    状态说明：
    - waiting: 等待扫码
    - scanned: 已扫码，等待确认
    - success: 扫码成功
    - expired: 二维码已过期
    - cancelled: 用户取消登录
    - verification_required: 需要手机验证
    - not_found: 会话不存在
    - already_processed: 已处理过
    """
    try:
        # 检查是否已处理过
        if session_id in PROCESSED_SESSIONS:
            processed_info = PROCESSED_SESSIONS[session_id]
            return _build_processed_response(processed_info)
        
        status_info = qr_login_manager.get_session_status(session_id)
        status = status_info.get("status")
        
        # 如果扫码成功，自动创建或更新账号
        if status == "success":
            meta = SESSION_META.get(session_id, {})
            owner_id = meta.get("owner_id", current_user.id)
            pending_account_id = meta.get("pending_account_id")

            # 使用锁防止并发处理
            lock = _get_session_lock(session_id)
            async with lock:
                # 双重检查
                if session_id in PROCESSED_SESSIONS:
                    processed_info = PROCESSED_SESSIONS[session_id]
                    return _build_processed_response(processed_info)

                cookies_info = qr_login_manager.get_session_cookies(session_id)
                if cookies_info:
                    cookies_str = cookies_info.get("cookies", "")
                    unb = cookies_info.get("unb")

                    try:
                        account_service = AccountService(db)
                        account, is_new_account = await account_service.upsert_account_from_qr(
                            owner_id=owner_id,
                            cookies=cookies_str,
                            unb=unb,
                            target_account_id=pending_account_id,
                        )
                    except AccountLimitExceededError as exc:
                        message = str(exc)
                        PROCESSED_SESSIONS[session_id] = {
                            "status": "failed",
                            "message": message,
                            "account_id": unb or "",
                            "is_new_account": True,
                        }
                        _cleanup_session(session_id)
                        return ApiResponse(
                            success=True,
                            message=message,
                            data={
                                "status": "failed",
                                "message": message,
                                "account_info": {
                                    "account_id": unb or "",
                                    "is_new_account": True,
                                }
                            }
                        )

                    logger.info(
                        f"扫码登录：{'创建新账号' if is_new_account else '更新现有账号'} {account.account_id}"
                    )
                    
                    # 调用 WebSocket 服务启动账号任务
                    try:
                        from app.core.config import get_settings
                        settings = get_settings()
                        client = get_http_client()
                        
                        # 构建请求参数
                        request_data = {
                            "cookie_value": cookies_str,
                            "user_id": owner_id
                        }
                        
                        if is_new_account:
                            # 新账号：启动任务
                            response = await client.post(
                                f"{settings.websocket_service_url}/internal/accounts/{account.account_id}/start",
                                json=request_data
                            )
                            if response.get("success"):
                                logger.info(f"扫码登录：新账号WebSocket任务已启动 {account.account_id}")
                            else:
                                logger.warning(f"扫码登录：启动WebSocket任务失败 {account.account_id}: {response.get('message')}")
                        else:
                            # 现有账号：重启任务
                            response = await client.post(
                                f"{settings.websocket_service_url}/internal/accounts/{account.account_id}/restart",
                                json=request_data
                            )
                            if response.get("success"):
                                logger.info(f"扫码登录：现有账号WebSocket任务已重启 {account.account_id}")
                            else:
                                logger.warning(f"扫码登录：重启WebSocket任务失败 {account.account_id}: {response.get('message')}")
                    except Exception as ws_e:
                        logger.error(f"扫码登录：调用WebSocket服务失败 {account.account_id}: {str(ws_e)}")
                    
                    # 记录已处理
                    PROCESSED_SESSIONS[session_id] = {
                        "account_id": account.account_id,
                        "is_new_account": is_new_account,
                    }
                    
                    _cleanup_session(session_id)
                    
                    return ApiResponse(
                        success=True,
                        message="扫码登录成功",
                        data={
                            "status": "success",
                            "account_info": {
                                "account_id": account.account_id,
                                "is_new_account": is_new_account,
                            }
                        }
                    )
        
        # 清理过期或取消的会话；占位记录（未转正的）一并删除，不留空壳
        if status in {"expired", "cancelled", "not_found"}:
            meta = SESSION_META.get(session_id, {})
            pending_account_id = meta.get("pending_account_id")
            if pending_account_id:
                try:
                    await AccountService(db).delete_pending_account(
                        meta.get("owner_id", current_user.id), pending_account_id
                    )
                    logger.info(f"会话{status}，已清理占位记录: {pending_account_id}")
                except Exception:
                    logger.exception(f"清理占位记录失败: {pending_account_id}")
            _cleanup_session(session_id)
            PROCESSED_SESSIONS.pop(session_id, None)
        
        return ApiResponse(
            success=True,
            message="查询成功",
            data=status_info
        )
        
    except Exception as e:
        logger.exception(f"查询扫码状态时发生异常: {session_id}")
        return ApiResponse(
            success=False,
            message=f"查询失败: {str(e)}",
        )


@router.get("/cookie/{session_id}")
async def get_qr_cookie(
    session_id: str,
    current_user: User = Depends(deps.get_current_active_user),
) -> ApiResponse:
    """
    获取登录Cookie
    
    仅在扫码成功后可用
    """
    try:
        cookies_info = qr_login_manager.get_session_cookies(session_id)
        
        if cookies_info:
            return ApiResponse(
                success=True,
                message="获取Cookie成功",
                data=cookies_info
            )
        else:
            return ApiResponse(
                success=False,
                message="Cookie不存在或会话未完成",
            )
    except Exception as e:
        logger.exception(f"获取Cookie时发生异常: {session_id}")
        return ApiResponse(
            success=False,
            message=f"获取Cookie失败: {str(e)}",
        )
