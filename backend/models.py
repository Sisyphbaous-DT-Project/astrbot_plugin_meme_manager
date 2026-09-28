import hashlib
import logging
import os
import re
import shutil
from pathlib import Path

from ..config import MEMES_DIR

logger = logging.getLogger(__name__)
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 单文件上传上限，前端提示文案与此保持一致


class DuplicateEmojiError(ValueError):
    """Raised when an uploaded emoji already exists in the target category."""

    def __init__(self, existing_filename: str):
        self.existing_filename = existing_filename
        super().__init__(f"同一分类中已存在相同文件：{existing_filename}")


class InvalidImageError(ValueError):
    """Raised when uploaded content is empty or not a decodable image."""


class FileTooLargeError(ValueError):
    """Raised when uploaded content exceeds MAX_UPLOAD_BYTES."""


class InvalidCategoryError(ValueError):
    """Raised when a category name is not a safe single-level directory name."""


class InvalidFilenameError(ValueError):
    """Raised when a filename is empty or contains unsafe parts."""


# 分类名/文件名中不允许出现的字符：路径分隔符、URL 语义字符、控制字符等
_UNSAFE_NAME_CHARS = re.compile(r'[/\\?#%:*<>"|\x00-\x1f\x7f]')


def _is_supported_image(filename: str) -> bool:
    return filename.lower().endswith(IMAGE_EXTENSIONS)


def validate_category_name(category: str) -> str:
    """校验分类名是安全的单层目录名，非法输入直接抛 InvalidCategoryError。"""
    if not isinstance(category, str):
        raise InvalidCategoryError("分类名必须是字符串")
    name = category.strip()
    if not name:
        raise InvalidCategoryError("分类名不能为空")
    if name in (".", "..") or name.startswith("."):
        raise InvalidCategoryError(f"非法分类名：{category!r}")
    if _UNSAFE_NAME_CHARS.search(name):
        raise InvalidCategoryError(f"分类名包含不允许的字符：{category!r}")
    return name


def validate_filename(filename: str) -> str:
    """校验文件名是安全的单层文件名（不含路径成分），非法直接抛 InvalidFilenameError。"""
    if not isinstance(filename, str):
        raise InvalidFilenameError("文件名必须是字符串")
    name = filename.strip()
    if not name or name in (".", "..") or name.startswith("."):
        raise InvalidFilenameError(f"非法文件名：{filename!r}")
    if name != Path(name).name or _UNSAFE_NAME_CHARS.search(name):
        raise InvalidFilenameError(f"文件名包含不允许的字符：{filename!r}")
    return name


def resolve_in_memes(category: str, filename: str | None = None) -> Path:
    """拼出分类/文件的真实路径，确认解析后仍在 MEMES_DIR 内（防 .. 与软链接逃逸）。"""
    safe_category = validate_category_name(category)
    base = Path(MEMES_DIR).resolve()
    target = base / safe_category
    if filename is not None:
        target = target / validate_filename(filename)
    resolved = target.resolve()
    if resolved != base and base not in resolved.parents:
        raise InvalidCategoryError(f"路径越界：{category!r}")
    return resolved


def sanitize_filename(filename: str) -> str | None:
    """清理用户上传的文件名：去掉路径成分和控制字符，保留中文主体。

    返回清理后的文件名；如果没有可用的主体部分则返回 None（由上层生成默认名）。
    扩展名的最终以内容嗅探为准，这里只保留原扩展名作为参考。
    """
    if not filename:
        return None
    name = Path(filename).name.strip().strip(".")
    # 去掉控制字符与路径分隔符残留
    name = re.sub(r'[/\\?#%:*<>"|\x00-\x1f\x7f]', "", name).strip()
    if not name:
        return None
    # 主体为空（如 ".jpg"、"..."）视为无效
    if not Path(name).stem:
        return None
    return name


def _get_category_path(category: str) -> Path:
    """分类目录路径：校验名称并解析真实路径，越界（..、软链接逃逸）直接抛错。"""
    return resolve_in_memes(category)


def _iter_category_image_paths(category_path: Path) -> list[Path]:
    if not category_path.is_dir():
        return []
    return [
        path
        for path in category_path.iterdir()
        if path.is_file() and _is_supported_image(path.name)
    ]


def _calculate_file_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _find_duplicate_image(category_path: Path, content_hash: str) -> Path | None:
    for existing_path in _iter_category_image_paths(category_path):
        try:
            if _calculate_file_hash(existing_path.read_bytes()) == content_hash:
                return existing_path
        except OSError as exc:
            logger.warning(
                "读取现有文件失败，跳过判重: %s, 错误: %s", existing_path, exc
            )
    return None


def _build_available_file_path(category_path: Path, filename: str) -> Path:
    candidate = category_path / filename
    if not candidate.exists():
        return candidate

    suffix = Path(filename).suffix
    stem = Path(filename).stem
    index = 1
    while True:
        candidate = category_path / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


async def scan_emoji_folder():
    """扫描表情包文件夹，返回所有类别及其表情包"""
    emoji_data = {}
    if not os.path.exists(MEMES_DIR):
        os.makedirs(MEMES_DIR)
    for category in os.listdir(MEMES_DIR):
        category_path = _get_category_path(category)
        if not category_path.is_dir():
            continue

        emoji_data[category] = [
            path.name for path in _iter_category_image_paths(category_path)
        ]
    return emoji_data


def get_emoji_by_category(category):
    """获取指定类别下的所有表情包"""
    category_path = _get_category_path(category)
    if not category_path.is_dir():
        return []
    return [path.name for path in _iter_category_image_paths(category_path)]


def add_emoji_to_category(
    category: str, content: bytes, original_filename: str
) -> dict[str, object]:
    """添加表情包到指定类别（纯 bytes 接口）。

    Args:
        category: 类别名
        content: 文件内容字节
        original_filename: 用户上传时的原始文件名

    Returns:
        dict: {outcome, category, filename, size, mime_type, width, height, is_animated}

    Raises:
        InvalidCategoryError: 分类名非法
        FileTooLargeError: 超过大小上限
        InvalidImageError: 空内容或无法解析的图片
        DuplicateEmojiError: 同分类已存在相同内容
    """
    from .media import sniff_image  # 延迟导入避免环

    safe_category = validate_category_name(category)
    if not content:
        raise InvalidImageError("上传内容为空")
    if len(content) > MAX_UPLOAD_BYTES:
        raise FileTooLargeError(
            f"文件超过大小上限（{MAX_UPLOAD_BYTES // 1024 // 1024} MiB）"
        )

    info = None
    try:
        info = sniff_image(content)
    except ValueError as exc:
        raise InvalidImageError(str(exc)) from exc

    category_path = resolve_in_memes(safe_category)
    category_path.mkdir(parents=True, exist_ok=True)

    # 文件名：保留中文主体，扩展名以内容嗅探为准
    cleaned = sanitize_filename(original_filename or "")
    if cleaned is None:
        cleaned = f"image_{int(__import__('time').time())}"
    stem = Path(cleaned).stem or "image"
    filename = f"{stem}{info.extension}"

    # 内容去重：同分类同内容直接跳过
    content_hash = _calculate_file_hash(content)
    duplicate_path = _find_duplicate_image(category_path, content_hash)
    if duplicate_path is not None:
        logger.info(
            "跳过重复文件上传: 类别=%s, 上传名=%s, 已存在文件=%s",
            safe_category,
            original_filename,
            duplicate_path.name,
        )
        raise DuplicateEmojiError(duplicate_path.name)

    file_path = _build_available_file_path(category_path, filename)

    # 先写临时文件，确认完整后原子落盘为正式文件
    tmp_path = file_path.with_name(f".{file_path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp_path, "wb") as f:
            f.write(content)
        os.replace(tmp_path, file_path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise

    if not file_path.exists() or file_path.stat().st_size == 0:
        raise OSError(f"文件保存失败: {file_path}")

    logger.info("文件成功保存到 %s, 大小: %d 字节", file_path, len(content))
    return {
        "outcome": "added",
        "category": safe_category,
        "filename": file_path.name,
        "path": str(file_path),
        "size": len(content),
        "mime_type": info.mime_type,
        "width": info.width,
        "height": info.height,
        "is_animated": info.is_animated,
    }


def delete_emoji_from_category(category, image_file):
    """删除指定类别下的表情包"""
    try:
        image_path = resolve_in_memes(category, image_file)
    except (InvalidCategoryError, InvalidFilenameError):
        return False
    if not image_path.is_file() or not _is_supported_image(image_path.name):
        return False
    image_path.unlink()
    return True


def batch_delete_emojis(category: str, image_files: list[str]) -> dict[str, object]:
    """批量删除指定类别下的表情包。"""
    category_path = _get_category_path(category)
    if not category_path.is_dir():
        return {
            "category_exists": False,
            "deleted_files": [],
            "missing_files": [],
        }

    deleted_files = []
    missing_files = []
    for image_file in dict.fromkeys(image_files):
        if delete_emoji_from_category(category, image_file):
            deleted_files.append(Path(image_file).name)
        else:
            missing_files.append(Path(image_file).name)

    return {
        "category_exists": True,
        "deleted_files": deleted_files,
        "missing_files": missing_files,
    }


def move_emoji_to_category(
    source_category: str, image_file: str, target_category: str
) -> dict[str, object]:
    """将单个表情包移动到另一个类别。源与目标都做边界校验，非法输入直接抛错。"""
    source_category_path = resolve_in_memes(source_category)
    target_category_path = resolve_in_memes(target_category)
    image_name = validate_filename(image_file)
    if not source_category_path.is_dir():
        return {
            "source_category_exists": False,
            "target_category": target_category,
            "filename": image_name,
            "moved": False,
            "conflict": False,
            "missing": True,
        }

    target_category_path.mkdir(parents=True, exist_ok=True)

    source_image_path = resolve_in_memes(source_category, image_name)
    target_image_path = target_category_path / image_name

    if not source_image_path.is_file() or not _is_supported_image(
        source_image_path.name
    ):
        return {
            "source_category_exists": True,
            "target_category": target_category,
            "filename": image_name,
            "moved": False,
            "conflict": False,
            "missing": True,
        }

    if target_image_path.exists():
        return {
            "source_category_exists": True,
            "target_category": target_category,
            "filename": image_name,
            "moved": False,
            "conflict": True,
            "missing": False,
        }

    shutil.move(str(source_image_path), str(target_image_path))
    return {
        "source_category_exists": True,
        "source_category": source_category,
        "target_category": target_category,
        "filename": image_name,
        "moved": True,
        "conflict": False,
        "missing": False,
    }


def batch_move_emojis(
    source_category: str, image_files: list[str], target_category: str
) -> dict[str, object]:
    """批量将表情包移动到另一个类别。"""
    source_category_path = resolve_in_memes(source_category)
    resolve_in_memes(target_category)  # 目标分类先校验，非法直接抛错
    if not source_category_path.is_dir():
        return {
            "source_category_exists": False,
            "moved_files": [],
            "missing_files": [],
            "conflicting_files": [],
        }

    moved_files = []
    missing_files = []
    conflicting_files = []

    for image_file in dict.fromkeys(image_files):
        result = move_emoji_to_category(source_category, image_file, target_category)
        if result["moved"]:
            moved_files.append(result["filename"])
        elif result["conflict"]:
            conflicting_files.append(result["filename"])
        elif result["missing"]:
            missing_files.append(result["filename"])

    return {
        "source_category_exists": True,
        "source_category": source_category,
        "target_category": target_category,
        "moved_files": moved_files,
        "missing_files": missing_files,
        "conflicting_files": conflicting_files,
    }


def copy_emoji_to_category(
    source_category: str, image_file: str, target_category: str
) -> dict[str, object]:
    """将单个表情包复制到另一个类别。源与目标都做边界校验，非法输入直接抛错。"""
    source_category_path = resolve_in_memes(source_category)
    target_category_path = resolve_in_memes(target_category)
    image_name = validate_filename(image_file)
    if not source_category_path.is_dir():
        return {
            "source_category_exists": False,
            "target_category": target_category,
            "filename": image_name,
            "copied": False,
            "conflict": False,
            "missing": True,
        }

    target_category_path.mkdir(parents=True, exist_ok=True)

    source_image_path = resolve_in_memes(source_category, image_name)
    target_image_path = target_category_path / image_name

    if not source_image_path.is_file() or not _is_supported_image(
        source_image_path.name
    ):
        return {
            "source_category_exists": True,
            "target_category": target_category,
            "filename": image_name,
            "copied": False,
            "conflict": False,
            "missing": True,
        }

    if target_image_path.exists():
        return {
            "source_category_exists": True,
            "target_category": target_category,
            "filename": image_name,
            "copied": False,
            "conflict": True,
            "missing": False,
        }

    shutil.copy2(source_image_path, target_image_path)
    return {
        "source_category_exists": True,
        "source_category": source_category,
        "target_category": target_category,
        "filename": image_name,
        "copied": True,
        "conflict": False,
        "missing": False,
    }


def batch_copy_emojis(
    source_category: str, image_files: list[str], target_category: str
) -> dict[str, object]:
    """批量将表情包复制到另一个类别。"""
    source_category_path = resolve_in_memes(source_category)
    resolve_in_memes(target_category)
    if not source_category_path.is_dir():
        return {
            "source_category_exists": False,
            "copied_files": [],
            "missing_files": [],
            "conflicting_files": [],
        }

    copied_files = []
    missing_files = []
    conflicting_files = []

    for image_file in dict.fromkeys(image_files):
        result = copy_emoji_to_category(source_category, image_file, target_category)
        if result["copied"]:
            copied_files.append(result["filename"])
        elif result["conflict"]:
            conflicting_files.append(result["filename"])
        elif result["missing"]:
            missing_files.append(result["filename"])

    return {
        "source_category_exists": True,
        "source_category": source_category,
        "target_category": target_category,
        "copied_files": copied_files,
        "missing_files": missing_files,
        "conflicting_files": conflicting_files,
    }


def clear_category_emojis(category: str) -> dict[str, object]:
    """清空指定类别下的所有表情包，但保留类别目录和配置。

    分类名非法或目录软链接逃逸时抛 InvalidCategoryError，不做任何删除。
    """
    category_path = resolve_in_memes(category)
    if not category_path.is_dir():
        return {
            "category_exists": False,
            "deleted_files": [],
        }

    deleted_files = []
    for image_path in _iter_category_image_paths(category_path):
        image_path.unlink()
        deleted_files.append(image_path.name)

    return {
        "category_exists": True,
        "deleted_files": deleted_files,
    }


def clear_all_emojis() -> dict[str, object]:
    """清空所有类别中的表情包，但保留目录和配置。

    遍历时跳过解析后越界的目录（例如指向图库外的软链接），不做任何删除。
    """
    deleted_by_category = {}
    memes_root = Path(MEMES_DIR).resolve()
    if not memes_root.exists():
        return {"deleted_by_category": deleted_by_category}

    for category_path in memes_root.iterdir():
        if not category_path.is_dir():
            continue
        try:
            resolved = category_path.resolve()
            if resolved != memes_root and memes_root not in resolved.parents:
                logger.warning(
                    "清空全部时跳过越界目录（可能是外部软链接）: %s", category_path
                )
                continue
        except OSError:
            continue
        result = clear_category_emojis(category_path.name)
        deleted_files = result["deleted_files"]
        if deleted_files:
            deleted_by_category[category_path.name] = len(deleted_files)

    return {"deleted_by_category": deleted_by_category}


def update_emoji_in_category(category, old_image_file, new_image_file):
    """更新（替换）表情包文件"""
    category_path = os.path.join(MEMES_DIR, category)

    if not os.path.isdir(category_path):
        return False
    old_image_path = os.path.join(category_path, old_image_file)
    if os.path.exists(old_image_path):
        os.remove(old_image_path)
        cleaned = sanitize_filename(new_image_file.filename)
        if cleaned is None:
            return False
        target_path = os.path.join(category_path, cleaned)
        new_image_file.save(target_path)
        return True
    return False
