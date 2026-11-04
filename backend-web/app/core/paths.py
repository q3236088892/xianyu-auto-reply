"""
路径配置模块

功能：
1. 统一管理项目中的文件路径
2. 提供上传目录、静态文件目录等配置
3. 确保目录存在
"""
from __future__ import annotations

import os
from pathlib import Path

# 项目根目录 (backend-web/)
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 静态文件根目录 - 与 _bootstrap.py 的挂载点(settings.static_dir)同源:
# 显式 STATIC_DIR > 默认 "static"(相对进程cwd解析, run_release 下 cwd=app/)。
# 历史上这里回退到 backend-web/static, 与挂载的 app/static 劈叉,
# 导致"上传成功但预览裂图"; portable 部署器已注入持久 STATIC_DIR 根治。
def _resolve_static_root() -> Path:
    env_dir = os.environ.get("STATIC_DIR", "").strip()
    if env_dir:
        p = Path(env_dir)
        return p if p.is_absolute() else (Path.cwd() / p).resolve()
    try:
        from app.core.config import get_settings
        cfg_dir = str(get_settings().static_dir or "static")
        p = Path(cfg_dir)
        # 与 _bootstrap 一致: StaticFiles 对相对路径按进程cwd解析
        return p if p.is_absolute() else (Path.cwd() / p).resolve()
    except Exception:
        return (Path.cwd() / "static").resolve()


STATIC_ROOT = _resolve_static_root()

# 上传文件目录 - 统一使用 static/uploads/
UPLOADS_ROOT = STATIC_ROOT / "uploads"

# 上传子目录
UPLOADS_IMAGES = UPLOADS_ROOT / "images"      # 图片上传目录
UPLOADS_KEYWORDS = UPLOADS_ROOT / "keywords"  # 关键词图片目录
UPLOADS_FACE = UPLOADS_ROOT / "face"          # 人脸验证截图目录
UPLOADS_PRODUCTS = UPLOADS_ROOT / "products"  # 商品发布图片目录


def ensure_upload_dirs() -> None:
    """确保所有上传目录存在"""
    dirs = [
        STATIC_ROOT,
        UPLOADS_ROOT,
        UPLOADS_IMAGES,
        UPLOADS_KEYWORDS,
        UPLOADS_FACE,
        UPLOADS_PRODUCTS,
    ]
    for dir_path in dirs:
        dir_path.mkdir(parents=True, exist_ok=True)


def get_upload_path(subdir: str = "images") -> Path:
    """获取上传目录路径
    
    Args:
        subdir: 子目录名称 (images/keywords/face)
        
    Returns:
        上传目录路径
    """
    ensure_upload_dirs()
    
    subdirs = {
        "images": UPLOADS_IMAGES,
        "keywords": UPLOADS_KEYWORDS,
        "face": UPLOADS_FACE,
        "products": UPLOADS_PRODUCTS,
    }
    return subdirs.get(subdir, UPLOADS_ROOT)


def get_static_url(filepath: str) -> str:
    """将文件路径转换为静态URL
    
    Args:
        filepath: 文件路径（相对于static目录）
        
    Returns:
        静态文件URL
    """
    # 移除可能的前缀
    filepath = filepath.replace("\\", "/")
    if filepath.startswith("static/"):
        filepath = filepath[7:]
    elif filepath.startswith("/static/"):
        filepath = filepath[8:]
    
    return f"/static/{filepath}"


__all__ = [
    "PROJECT_ROOT",
    "STATIC_ROOT", 
    "UPLOADS_ROOT",
    "UPLOADS_IMAGES",
    "UPLOADS_KEYWORDS",
    "UPLOADS_FACE",
    "UPLOADS_PRODUCTS",
    "ensure_upload_dirs",
    "get_upload_path",
    "get_static_url",
]
