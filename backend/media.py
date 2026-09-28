"""图片内容嗅探、尺寸/帧数读取、缩略图缓存与原图 base64 读取。

本模块的函数都是同步 CPU/IO 操作，调用方（dashboard handler）应通过
asyncio.to_thread 执行，避免阻塞机器人主事件循环。
"""

import base64
import hashlib
import io
import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image as PILImage

from ..config import PLUGIN_DATA_DIR

logger = logging.getLogger(__name__)

# 支持格式：PIL format -> (扩展名, mime)
_FORMAT_MAP = {
    "PNG": (".png", "image/png"),
    "JPEG": (".jpg", "image/jpeg"),
    "GIF": (".gif", "image/gif"),
    "WEBP": (".webp", "image/webp"),
}

THUMBNAIL_MAX_SIDE = 256
THUMBNAIL_CACHE_LIMIT = 200
THUMBNAILS_DIR = PLUGIN_DATA_DIR / "thumbnails"


@dataclass
class ImageInfo:
    """一张图片的真实内容信息（来自内容嗅探，不以后缀为准）。"""

    format: str  # PIL format，如 "GIF"
    extension: str  # 以真实格式为准的扩展名，如 ".gif"
    mime_type: str
    width: int
    height: int
    n_frames: int
    is_animated: bool


def sniff_image(content: bytes) -> ImageInfo:
    """用 PIL 验证并读取图片信息。无法解析时抛出 ValueError（上层转成 InvalidImageError）。

    PIL 的 open() 是惰性读取，能识别头信息不代表图片完整；
    这里逐帧完整解码验证，损坏/截断的文件不允许进入图库。
    """
    if not content:
        raise ValueError("内容为空")
    try:
        with PILImage.open(io.BytesIO(content)) as img:
            fmt = (img.format or "").upper()
            if fmt not in _FORMAT_MAP:
                raise ValueError(f"不支持的图片格式: {fmt or '未知'}")
            n_frames = getattr(img, "n_frames", 1) or 1
            width, height = img.width, img.height
            # 逐帧解码验证完整性（尺寸上限已由调用方限制在 20 MiB）
            for frame_index in range(n_frames):
                img.seek(frame_index)
                img.load()
            extension, mime_type = _FORMAT_MAP[fmt]
            return ImageInfo(
                format=fmt,
                extension=extension,
                mime_type=mime_type,
                width=width,
                height=height,
                n_frames=n_frames,
                is_animated=n_frames > 1,
            )
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"无法解析的图片内容: {exc}") from exc


_INFO_CACHE_LIMIT = 500
_info_cache: dict[str, tuple[int, int, ImageInfo | None]] = {}
_validated_cache: dict[str, tuple[int, int, str]] = {}


def read_image_info(path: Path) -> ImageInfo | None:
    """读取磁盘上图片文件的信息，失败返回 None（不抛异常，供列表惰性读取用）。

    按 (路径, mtime, size) 建小缓存，文件变化自动失效，避免列表/缩略图反复解码。
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    key = str(path)
    cached = _info_cache.get(key)
    if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return cached[2]

    info = _read_image_info_uncached(path)
    _info_cache[key] = (stat.st_mtime_ns, stat.st_size, info)
    if len(_info_cache) > _INFO_CACHE_LIMIT:
        # 简单清理：删掉一半最早的条目（dict 保序）
        for old_key in list(_info_cache)[: _INFO_CACHE_LIMIT // 2]:
            _info_cache.pop(old_key, None)
    return info


def _read_image_info_uncached(path: Path) -> ImageInfo | None:
    try:
        with PILImage.open(path) as img:
            fmt = (img.format or "").upper()
            if fmt not in _FORMAT_MAP:
                return None
            n_frames = getattr(img, "n_frames", 1) or 1
            extension, mime_type = _FORMAT_MAP[fmt]
            return ImageInfo(
                format=fmt,
                extension=extension,
                mime_type=mime_type,
                width=img.width,
                height=img.height,
                n_frames=n_frames,
                is_animated=n_frames > 1,
            )
    except Exception as exc:
        logger.warning("读取图片信息失败 %s: %s", path, exc)
        return None


def _thumbnail_cache_key(path: Path) -> str:
    stat = path.stat()
    raw = f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _thumbnail_cache_path(category: str, key: str) -> Path:
    return THUMBNAILS_DIR / category / f"{key}.png"


def _evict_thumbnails_if_needed() -> None:
    """缩略图缓存惰性清理：超过上限时按修改时间删掉最旧的一批。"""
    try:
        if not THUMBNAILS_DIR.is_dir():
            return
        all_thumbs = sorted(
            THUMBNAILS_DIR.rglob("*.png"),
            key=lambda p: p.stat().st_mtime,
        )
        excess = len(all_thumbs) - THUMBNAIL_CACHE_LIMIT
        for old in all_thumbs[: max(excess, 0)]:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception as exc:
        logger.warning("缩略图缓存清理失败: %s", exc)


def make_thumbnail(category: str, src_path: Path) -> bytes | None:
    """生成/读取静态首帧缩略图（PNG 保留透明），带磁盘缓存。

    仅用于网格显示，不修改也不替代实际存储与发送的原图。
    """
    try:
        key = _thumbnail_cache_key(src_path)
        cache_path = _thumbnail_cache_path(category, key)
        if cache_path.is_file():
            return cache_path.read_bytes()

        with PILImage.open(src_path) as img:
            # 动图取首帧；保留透明通道
            if getattr(img, "is_animated", False):
                img.seek(0)
            if img.mode == "P":
                img = img.convert("RGBA")
            elif img.mode not in ("RGB", "RGBA", "L", "LA"):
                img = img.convert("RGBA")
            img.thumbnail((THUMBNAIL_MAX_SIDE, THUMBNAIL_MAX_SIDE))
            buf = io.BytesIO()
            img.save(buf, "PNG")

        data = buf.getvalue()
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(data)
            _evict_thumbnails_if_needed()
        except OSError as exc:
            logger.warning("缩略图缓存写入失败: %s", exc)
        return data
    except Exception as exc:
        logger.warning("生成缩略图失败 %s: %s", src_path, exc)
        return None


def read_original_b64(path: Path) -> tuple[str, str]:
    """读取原图字节并 base64 编码。返回 (mime_type, base64_str)。

    GIF 等多帧文件原样返回完整字节，不转静态图。
    """
    stat = path.stat()
    key = str(path)
    cached = _validated_cache.get(key)
    raw = path.read_bytes()
    if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
        mime = cached[2]
    else:
        mime = sniff_image(raw).mime_type
        _validated_cache[key] = (stat.st_mtime_ns, stat.st_size, mime)
        if len(_validated_cache) > _INFO_CACHE_LIMIT:
            for old_key in list(_validated_cache)[: _INFO_CACHE_LIMIT // 2]:
                _validated_cache.pop(old_key, None)
    return mime, base64.b64encode(raw).decode("ascii")
