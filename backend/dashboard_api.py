"""meme_manager 的 AstrBot Dashboard 插件页 Web API。

所有 handler 使用 astrbot.api.web 的 request/response，鉴权由 Dashboard 统一完成；
业务结果直接平铺返回（顶层不放 data 键，避免 bridge 的 data 解包吃掉其他字段），
真正的错误用 error_response("人类可读消息", status_code=4xx)。
"""

import asyncio
import base64
import logging
import os

from astrbot.api.web import (
    PluginUploadFile,
    error_response,
    file_response,
    json_response,
    request,
)

from ..config import MEMES_DIR
from .media import make_thumbnail, read_image_info, read_original_b64
from .models import (
    MAX_UPLOAD_BYTES,
    DuplicateEmojiError,
    FileTooLargeError,
    InvalidCategoryError,
    InvalidFilenameError,
    InvalidImageError,
    add_emoji_to_category,
    batch_copy_emojis,
    batch_delete_emojis,
    batch_move_emojis,
    clear_all_emojis,
    clear_category_emojis,
    resolve_in_memes,
    validate_category_name,
    validate_filename,
    _is_supported_image,
)

logger = logging.getLogger(__name__)

# 原图 base64 预览的大小上限（与上传上限同源）
MAX_PREVIEW_BYTES = MAX_UPLOAD_BYTES
CHUNK_SIZE = 1024 * 1024


def overwrite_sync_running(plugin) -> bool:
    """覆盖类图床同步进行中时，拒绝网页图库写操作（简单规则，不做跨进程锁）。"""
    task = getattr(plugin, "_sync_task", None)
    info = getattr(plugin, "_sync_info", {}) or {}
    return bool(
        task
        and not task.done()
        and info.get("direction") in ("overwrite_to_remote", "overwrite_from_remote")
    )


def _sync_conflict_response(plugin):
    if overwrite_sync_running(plugin):
        return error_response(
            "图床覆盖同步进行中，请完成后再操作图库", status_code=409
        )
    return None


def _error_response_for(exc: Exception):
    """把业务异常映射为明确的错误响应。"""
    if isinstance(exc, FileTooLargeError):
        return error_response(str(exc), status_code=413)
    if isinstance(exc, DuplicateEmojiError):
        # 重复是预期内的业务结果，不走 error；这里兜底（正常应在上层单独处理）
        return json_response(
            {
                "outcome": "duplicate",
                "filename": exc.existing_filename,
                "message": str(exc),
            }
        )
    if isinstance(exc, (InvalidCategoryError, InvalidFilenameError, InvalidImageError)):
        return error_response(str(exc), status_code=400)
    logger.error("dashboard api 内部错误: %s", exc, exc_info=True)
    return error_response(f"服务器内部错误: {exc}", status_code=500)


async def _read_upload_with_limit(upload: PluginUploadFile) -> bytes:
    """分块读取上传内容并计数，超限抛 FileTooLargeError。

    使用正式版与开发分支共有的 read(size)，不调用 save()，也不依赖其返回值。
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise FileTooLargeError(
                f"文件超过大小上限（{MAX_UPLOAD_BYTES // 1024 // 1024} MiB）"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _list_category_files(category: str) -> list[dict]:
    """列出一个分类下的图片文件基础信息（不含内容）。"""
    try:
        category_path = resolve_in_memes(category)
    except InvalidCategoryError:
        return []
    if not category_path.is_dir():
        return []
    items = []
    for path in sorted(category_path.iterdir()):
        if not path.is_file() or not _is_supported_image(path.name):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append(
            {
                "category": category,
                "name": path.name,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            }
        )
    return items


def _attach_image_meta(items: list[dict]) -> None:
    """惰性补充尺寸/帧数信息（就地填充；失败时字段为 None）。"""
    for item in items:
        try:
            path = resolve_in_memes(item["category"], item["name"])
            info = read_image_info(path)
        except (InvalidCategoryError, InvalidFilenameError):
            info = None
        if info is None:
            item["width"] = None
            item["height"] = None
            item["is_animated"] = None
        else:
            item["width"] = info.width
            item["height"] = info.height
            item["is_animated"] = info.is_animated


def register_dashboard_apis(plugin) -> None:
    """把 meme_manager 的 Dashboard API 注册到 AstrBot。

    Args:
        plugin: MemeSender 实例，handler 直接持有它的共享状态
        （category_manager / img_sync / reload_emotions / 锁）。
    """

    async def overview():
        """分类列表 + 描述 + 数量 + 总数；不含任何图片内容。"""
        descriptions = plugin.category_manager.get_descriptions()
        local_categories = plugin.category_manager.get_local_categories()
        categories = sorted(set(descriptions) | local_categories)
        result = []
        total = 0
        for category in categories:
            files = await asyncio.to_thread(_list_category_files, category)
            count = len(files)
            total += count
            result.append(
                {
                    "name": category,
                    "description": descriptions.get(category, ""),
                    "count": count,
                }
            )
        return json_response(
            {
                "categories": result,
                "total": total,
                # 轻量标志，供页面决定高级区域展示；不触发远端访问
                "image_host_configured": bool(plugin.img_sync),
            }
        )

    async def list_images():
        """分页列出图片元信息；列表不携带图片内容。"""
        category = (request.query.get("category") or "").strip()
        query = (request.query.get("q") or "").strip().lower()
        try:
            offset = max(0, int(request.query.get("offset", "0") or 0))
            limit = min(100, max(1, int(request.query.get("limit", "48") or 48)))
        except ValueError:
            return error_response("offset/limit 参数不合法", status_code=400)

        if category:
            try:
                validate_category_name(category)
            except InvalidCategoryError as exc:
                return error_response(str(exc), status_code=400)
            categories = [category]
        else:
            descriptions = plugin.category_manager.get_descriptions()
            categories = sorted(
                set(descriptions) | plugin.category_manager.get_local_categories()
            )

        all_items: list[dict] = []
        for cat in categories:
            all_items.extend(await asyncio.to_thread(_list_category_files, cat))
        all_items.sort(key=lambda x: (x["category"], x["name"]))

        if query:
            all_items = [
                item
                for item in all_items
                if query in item["name"].lower() or query in item["category"].lower()
            ]

        total = len(all_items)
        page_items = all_items[offset : offset + limit]
        await asyncio.to_thread(_attach_image_meta, page_items)
        return json_response({"items": page_items, "total": total})

    async def thumbnail():
        """网格缩略图：静态首帧 PNG，带缓存；不动原图。"""
        category = request.query.get("category") or ""
        name = request.query.get("name") or ""
        try:
            path = resolve_in_memes(category, name)
        except (InvalidCategoryError, InvalidFilenameError) as exc:
            return error_response(str(exc), status_code=400)
        if not path.is_file() or not _is_supported_image(path.name):
            return error_response("图片不存在", status_code=404)

        def _build():
            data = make_thumbnail(category, path)
            info = read_image_info(path)
            return data, info

        data, info = await asyncio.to_thread(_build)
        if data is None:
            return error_response("缩略图生成失败", status_code=500)
        return json_response(
            {
                "content": base64.b64encode(data).decode("ascii"),
                "is_animated": bool(info and info.is_animated),
            }
        )

    async def original_image():
        """原图字节 base64（GIF 完整保留，不转静态图）。"""
        category = request.query.get("category") or ""
        name = request.query.get("name") or ""
        try:
            path = resolve_in_memes(category, name)
        except (InvalidCategoryError, InvalidFilenameError) as exc:
            return error_response(str(exc), status_code=400)
        if not path.is_file() or not _is_supported_image(path.name):
            return error_response("图片不存在", status_code=404)
        try:
            size = path.stat().st_size
        except OSError:
            return error_response("无法读取图片", status_code=500)
        if size > MAX_PREVIEW_BYTES:
            return error_response(
                "图片过大，请使用下载查看", status_code=413
            )

        def _read():
            mime, b64 = read_original_b64(path)
            info = read_image_info(path)
            return mime, b64, info, size

        try:
            mime, b64, info, size = await asyncio.to_thread(_read)
        except Exception as exc:
            logger.error("读取原图失败 %s: %s", path, exc)
            return error_response("读取图片失败", status_code=500)
        return json_response(
            {
                "content": b64,
                "mime": mime,
                "width": info.width if info else None,
                "height": info.height if info else None,
                "is_animated": bool(info and info.is_animated),
                "size": size,
            }
        )

    async def upload_image(category: str):
        """上传单张图片到指定分类（bridge.upload 的 file 字段）。"""
        conflict = _sync_conflict_response(plugin)
        if conflict is not None:
            return conflict
        try:
            safe_category = validate_category_name(category)
        except InvalidCategoryError as exc:
            return error_response(str(exc), status_code=400)

        files = await request.files()
        upload = files.get("file")
        if not isinstance(upload, PluginUploadFile):
            return error_response("没有找到上传的文件（字段名应为 file）", status_code=400)

        try:
            content = await _read_upload_with_limit(upload)
        except FileTooLargeError as exc:
            return error_response(str(exc), status_code=413)

        original_filename = upload.filename or ""
        async with plugin._library_lock:
            conflict = _sync_conflict_response(plugin)
            if conflict is not None:
                return conflict
            try:
                result = await asyncio.to_thread(
                    add_emoji_to_category, safe_category, content, original_filename
                )
            except DuplicateEmojiError as exc:
                return json_response(
                    {
                        "outcome": "duplicate",
                        "category": safe_category,
                        "filename": exc.existing_filename,
                        "message": "同一分类已存在相同图片",
                    }
                )
            except (FileTooLargeError, InvalidImageError, InvalidCategoryError) as exc:
                return _error_response_for(exc)
            except Exception as exc:
                return _error_response_for(exc)

        # 落盘成功后刷新运行时；文件已保存但刷新失败要如实区分，
        # 不能让用户以为需要重试（重试会因去重变成 duplicate）
        result.pop("path", None)
        try:
            await plugin.reload_emotions()
            result["refresh_ok"] = True
        except Exception as exc:
            logger.error("上传后刷新运行时失败: %s", exc)
            result["refresh_ok"] = False
            result["message"] = "图片已保存，但机器人状态刷新失败，建议重载插件"
        return json_response(result)

    plugin.context.register_web_api(
        "/meme_manager/overview", overview, ["GET"], "表情包总览"
    )
    plugin.context.register_web_api(
        "/meme_manager/images", list_images, ["GET"], "图片列表"
    )
    plugin.context.register_web_api(
        "/meme_manager/thumb", thumbnail, ["GET"], "图片缩略图"
    )
    plugin.context.register_web_api(
        "/meme_manager/image", original_image, ["GET"], "原图预览"
    )
    plugin.context.register_web_api(
        "/meme_manager/categories/<category>/images",
        upload_image,
        ["POST"],
        "上传图片到分类",
    )

    # ---------- 以下为整理 / 分类管理 / 图床同步接口 ----------

    def _parse_items(payload: dict) -> list[tuple[str, str]] | None:
        """解析 [{category, name}] 列表；含非法项时返回 None。"""
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            return None
        parsed = []
        for item in items:
            if not isinstance(item, dict):
                return None
            try:
                cat = validate_category_name(str(item.get("category") or ""))
                name = validate_filename(str(item.get("name") or ""))
            except (InvalidCategoryError, InvalidFilenameError):
                return None
            parsed.append((cat, name))
        return parsed

    async def delete_images():
        """批量删除（跨分类，分类+文件名双标识），逐项返回结果。"""
        conflict = _sync_conflict_response(plugin)
        if conflict is not None:
            return conflict
        payload = await request.json(default={})
        conflict = _sync_conflict_response(plugin)
        if conflict is not None:
            return conflict
        items = _parse_items(payload)
        if items is None:
            return error_response("items 参数不合法", status_code=400)

        deleted, missing, rejected = [], [], []
        async with plugin._library_lock:
            conflict = _sync_conflict_response(plugin)
            if conflict is not None:
                return conflict
            for cat, name in items:
                try:
                    result = await asyncio.to_thread(
                        batch_delete_emojis, cat, [name]
                    )
                    if result["deleted_files"]:
                        deleted.append({"category": cat, "name": name})
                    else:
                        missing.append({"category": cat, "name": name})
                except Exception as exc:
                    logger.error("删除失败 %s/%s: %s", cat, name, exc)
                    rejected.append(
                        {"category": cat, "name": name, "reason": str(exc)}
                    )
        if deleted:
            try:
                await plugin.reload_emotions()
            except Exception as exc:
                logger.error("删除后刷新运行时失败: %s", exc)
                return error_response(
                    "文件已删除但运行时刷新失败，请重载插件", status_code=500
                )
        return json_response(
            {"deleted": deleted, "missing": missing, "rejected": rejected}
        )

    async def _move_or_copy(payload: dict, op: str):
        conflict = _sync_conflict_response(plugin)
        if conflict is not None:
            return conflict
        items = _parse_items(payload)
        target = payload.get("target_category")
        if items is None or not isinstance(target, str):
            return error_response("items/target_category 参数不合法", status_code=400)
        try:
            target = validate_category_name(target)
            resolve_in_memes(target)  # 软链接逃逸也要拦住
        except InvalidCategoryError as exc:
            return error_response(str(exc), status_code=400)

        moved, missing, conflicting = [], [], []
        fn = batch_move_emojis if op == "move" else batch_copy_emojis
        done_key = "moved_files" if op == "move" else "copied_files"
        async with plugin._library_lock:
            conflict = _sync_conflict_response(plugin)
            if conflict is not None:
                return conflict
            for cat, name in items:
                if cat == target:
                    conflicting.append({"category": cat, "name": name})
                    continue
                try:
                    result = await asyncio.to_thread(fn, cat, [name], target)
                    if result[done_key]:
                        moved.append(
                            {"category": cat, "name": name, "target": target}
                        )
                    elif result["conflicting_files"]:
                        conflicting.append({"category": cat, "name": name})
                    else:
                        missing.append({"category": cat, "name": name})
                except (InvalidCategoryError, InvalidFilenameError) as exc:
                    conflicting.append(
                        {"category": cat, "name": name, "reason": str(exc)}
                    )
                except Exception as exc:
                    logger.error("%s 失败 %s/%s: %s", op, cat, name, exc)
                    conflicting.append(
                        {"category": cat, "name": name, "reason": str(exc)}
                    )
        if moved:
            try:
                await plugin.reload_emotions()
            except Exception as exc:
                logger.error("%s 后刷新运行时失败: %s", op, exc)
                return error_response(
                    "文件已变更但运行时刷新失败，请重载插件", status_code=500
                )
        result_key = "moved" if op == "move" else "copied"
        return json_response(
            {
                result_key: moved,
                "missing": missing,
                "conflicting": conflicting,
                "target_category": target,
            }
        )

    async def move_images():
        return await _move_or_copy(await request.json(default={}), "move")

    async def copy_images():
        return await _move_or_copy(await request.json(default={}), "copy")

    async def create_category():
        """新建分类：建目录 + 写说明 + 刷新运行时。"""
        conflict = _sync_conflict_response(plugin)
        if conflict is not None:
            return conflict
        payload = await request.json(default={})
        name = payload.get("name")
        description = str(payload.get("description") or "请添加描述")
        if not isinstance(name, str):
            return error_response("name 参数不合法", status_code=400)
        try:
            name = validate_category_name(name)
        except InvalidCategoryError as exc:
            return error_response(str(exc), status_code=400)
        async with plugin._library_lock:
            conflict = _sync_conflict_response(plugin)
            if conflict is not None:
                return conflict
            descriptions = plugin.category_manager.get_descriptions()
            # 存在性检查与写入在同一临界区完成
            if (
                name in descriptions
                or name in plugin.category_manager.get_local_categories()
            ):
                return error_response(f"分类「{name}」已存在", status_code=409)
            # 记录目录是不是本次新建的，保存失败只撤销本次新建且仍为空的目录
            dir_path = os.path.join(MEMES_DIR, name)
            created_dir = not os.path.exists(dir_path)
            try:
                os.makedirs(dir_path, exist_ok=True)
            except OSError as exc:
                return error_response(f"创建分类目录失败: {exc}", status_code=500)
            if not plugin.category_manager.update_description(name, description):
                if created_dir:
                    try:
                        os.rmdir(dir_path)  # 仅在空目录时可移除，避免误删
                    except OSError:
                        pass
                return error_response("保存分类说明失败，已撤销本次新建", status_code=500)
            try:
                await plugin.reload_emotions()
            except Exception as exc:
                logger.error("新建分类后刷新运行时失败: %s", exc)
                return error_response(
                    "分类已创建但运行时刷新失败，请重载插件", status_code=500
                )
        return json_response({"created": True, "name": name})

    async def update_category_description(category: str):
        conflict = _sync_conflict_response(plugin)
        if conflict is not None:
            return conflict
        payload = await request.json(default={})
        description = payload.get("description")
        if not isinstance(description, str) or not description.strip():
            return error_response("description 不能为空", status_code=400)
        try:
            category = validate_category_name(category)
        except InvalidCategoryError as exc:
            return error_response(str(exc), status_code=400)
        async with plugin._library_lock:
            conflict = _sync_conflict_response(plugin)
            if conflict is not None:
                return conflict
            if not plugin.category_manager.update_description(
                category, description.strip()
            ):
                return error_response("保存分类说明失败", status_code=500)
            try:
                await plugin.reload_emotions()
            except Exception as exc:
                logger.error("更新说明后刷新运行时失败: %s", exc)
                return error_response(
                    "说明已保存但运行时刷新失败，请重载插件", status_code=500
                )
        return json_response({"updated": True, "category": category})

    async def rename_category(category: str):
        conflict = _sync_conflict_response(plugin)
        if conflict is not None:
            return conflict
        payload = await request.json(default={})
        new_name = payload.get("new_name")
        if not isinstance(new_name, str):
            return error_response("new_name 参数不合法", status_code=400)
        async with plugin._library_lock:
            conflict = _sync_conflict_response(plugin)
            if conflict is not None:
                return conflict
            if not plugin.category_manager.rename_category(category, new_name):
                return error_response(
                    "重命名失败：目标名已存在或不合法，或保存未成功", status_code=409
                )
            try:
                await plugin.reload_emotions()
            except Exception as exc:
                logger.error("重命名后刷新运行时失败: %s", exc)
                return error_response(
                    "分类已重命名但运行时刷新失败，请重载插件", status_code=500
                )
        return json_response({"renamed": True, "new_name": new_name})

    async def delete_category(category: str):
        """删除分类本身 + 其图片 + 说明。"""
        conflict = _sync_conflict_response(plugin)
        if conflict is not None:
            return conflict
        async with plugin._library_lock:
            conflict = _sync_conflict_response(plugin)
            if conflict is not None:
                return conflict
            count = len(await asyncio.to_thread(_list_category_files, category))
            if not plugin.category_manager.delete_category(category):
                return error_response("删除分类失败", status_code=500)
            try:
                await plugin.reload_emotions()
            except Exception as exc:
                logger.error("删除分类后刷新运行时失败: %s", exc)
                return error_response(
                    "分类已删除但运行时刷新失败，请重载插件", status_code=500
                )
        return json_response(
            {"deleted": True, "category": category, "deleted_files": count}
        )

    async def clear_category(category: str):
        """清空分类下所有图片，保留分类与说明。"""
        conflict = _sync_conflict_response(plugin)
        if conflict is not None:
            return conflict
        async with plugin._library_lock:
            conflict = _sync_conflict_response(plugin)
            if conflict is not None:
                return conflict
            try:
                result = await asyncio.to_thread(clear_category_emojis, category)
            except InvalidCategoryError as exc:
                return error_response(str(exc), status_code=400)
            if not result["category_exists"]:
                return error_response("分类不存在", status_code=404)
            try:
                await plugin.reload_emotions()
            except Exception as exc:
                logger.error("清空分类后刷新运行时失败: %s", exc)
                return error_response(
                    "图片已清空但运行时刷新失败，请重载插件", status_code=500
                )
        return json_response(
            {
                "cleared": True,
                "category": category,
                "deleted_count": len(result["deleted_files"]),
            }
        )

    async def clear_all():
        """清空全部图片，保留分类与说明（高级危险操作）。"""
        conflict = _sync_conflict_response(plugin)
        if conflict is not None:
            return conflict
        async with plugin._library_lock:
            conflict = _sync_conflict_response(plugin)
            if conflict is not None:
                return conflict
            result = await asyncio.to_thread(clear_all_emojis)
            try:
                await plugin.reload_emotions()
            except Exception as exc:
                logger.error("清空全部后刷新运行时失败: %s", exc)
                return error_response(
                    "图片已清空但运行时刷新失败，请重载插件", status_code=500
                )
        return json_response(
            {
                "cleared": True,
                "deleted_count": sum(result["deleted_by_category"].values()),
            }
        )

    async def download_image():
        """原图文件下载（超大图或用户主动保存用）。"""
        category = request.query.get("category") or ""
        name = request.query.get("name") or ""
        try:
            path = resolve_in_memes(category, name)
        except (InvalidCategoryError, InvalidFilenameError) as exc:
            return error_response(str(exc), status_code=400)
        if not path.is_file() or not _is_supported_image(path.name):
            return error_response("图片不存在", status_code=404)
        return file_response(path, filename=path.name)

    async def sync_status():
        """图床同步状态；未配置时返回 configured=false，不报错。"""
        if not plugin.img_sync:
            return json_response({"configured": False})
        try:
            status = await asyncio.to_thread(plugin.img_sync.check_status)
        except Exception as exc:
            logger.error("获取同步状态失败: %s", exc)
            return error_response(f"获取同步状态失败: {exc}", status_code=500)
        return json_response(
            {
                "configured": True,
                "to_upload_count": len(status.get("to_upload", [])),
                "to_download_count": len(status.get("to_download", [])),
                "to_delete_remote_count": len(status.get("to_delete_remote", [])),
                "to_delete_local_count": len(status.get("to_delete_local", [])),
                "provider": getattr(plugin.img_sync, "provider_type", "未知"),
            }
        )

    async def sync_start():
        """启动图床同步（用户主动触发，统一互斥入口）。"""
        if not plugin.img_sync:
            return error_response("图床服务未配置", status_code=400)
        payload = await request.json(default={})
        direction = payload.get("direction")
        if direction not in (
            "upload",
            "download",
            "overwrite_to_remote",
            "overwrite_from_remote",
        ):
            return error_response("direction 参数不合法", status_code=400)
        ok, message = await plugin._start_image_sync(direction)
        if not ok:
            return error_response(message, status_code=409)
        return json_response({"started": True, "direction": direction})

    async def sync_process():
        """轮询同步进度：只读快照，不清空任何状态。"""
        info = plugin._sync_info
        last = plugin._sync_last_result
        running = bool(plugin._sync_task and not plugin._sync_task.done())
        return json_response(
            {
                "running": running,
                "direction": info.get("direction") if running else None,
                "finished": bool(last),
                "success": last.get("success") if last else None,
                "message": last.get("message") if last else None,
            }
        )

    plugin.context.register_web_api(
        "/meme_manager/images/delete", delete_images, ["POST"], "批量删除图片"
    )
    plugin.context.register_web_api(
        "/meme_manager/images/move", move_images, ["POST"], "批量移动图片"
    )
    plugin.context.register_web_api(
        "/meme_manager/images/copy", copy_images, ["POST"], "批量复制图片"
    )
    plugin.context.register_web_api(
        "/meme_manager/categories", create_category, ["POST"], "新建分类"
    )
    plugin.context.register_web_api(
        "/meme_manager/categories/<category>/description",
        update_category_description,
        ["POST"],
        "更新分类说明",
    )
    plugin.context.register_web_api(
        "/meme_manager/categories/<category>/rename",
        rename_category,
        ["POST"],
        "重命名分类",
    )
    plugin.context.register_web_api(
        "/meme_manager/categories/<category>/delete",
        delete_category,
        ["POST"],
        "删除分类",
    )
    plugin.context.register_web_api(
        "/meme_manager/categories/<category>/clear",
        clear_category,
        ["POST"],
        "清空分类图片",
    )
    plugin.context.register_web_api(
        "/meme_manager/categories/clear_all", clear_all, ["POST"], "清空全部图片"
    )
    plugin.context.register_web_api(
        "/meme_manager/image/download", download_image, ["GET"], "下载原图"
    )
    plugin.context.register_web_api(
        "/meme_manager/sync/status", sync_status, ["GET"], "图床同步状态"
    )
    plugin.context.register_web_api(
        "/meme_manager/sync/start", sync_start, ["POST"], "启动图床同步"
    )
    plugin.context.register_web_api(
        "/meme_manager/sync/process", sync_process, ["GET"], "图床同步进度"
    )
