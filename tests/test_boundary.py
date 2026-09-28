"""R01 回归：清空/移动/复制的真实 handler 必须守住图库边界。

不只是测校验工具，而是构造真实 plugin 依赖、真实注册、真实 handler 调用。
"""

import os

import pytest

from astrbot_plugin_meme_manager.backend.models import (
    InvalidCategoryError,
    add_emoji_to_category,
    clear_all_emojis,
    clear_category_emojis,
)
from astrbot_plugin_meme_manager.config import MEMES_DIR


def test_clear_rejects_dotdot(tmp_path, make_image):
    """清空分类为 .. 时，图库外文件不能被删。"""
    sentinel = tmp_path / "outside_gallery.png"
    sentinel.write_bytes(make_image("PNG"))
    with pytest.raises(InvalidCategoryError):
        clear_category_emojis("..")
    assert sentinel.exists()


def test_clear_rejects_symlinked_category(tmp_path, make_image):
    """分类目录是指向图库外的软链接时，清空不能顺着链接删外部文件。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.png").write_bytes(make_image("PNG"))
    link = os.path.join(str(MEMES_DIR), "zz_link_clear")
    os.symlink(str(outside), link)
    try:
        with pytest.raises(InvalidCategoryError):
            clear_category_emojis("zz_link_clear")
        assert (outside / "keep.png").exists()
    finally:
        os.unlink(link)


def test_clear_all_skips_symlinked_dir(tmp_path, make_image):
    """整库清空跳过指向图库外的目录，正常分类照常清空。"""
    outside = tmp_path / "outside2"
    outside.mkdir()
    (outside / "keep.png").write_bytes(make_image("PNG"))
    link = os.path.join(str(MEMES_DIR), "zz_link_all")
    os.symlink(str(outside), link)

    normal = "zz_normal_clear"
    add_emoji_to_category(normal, make_image("PNG"), "n.png")
    try:
        clear_all_emojis()
        assert (outside / "keep.png").exists()  # 软链接目标未被误删
        assert get_files(normal) == []  # 正常分类已清空
    finally:
        os.unlink(link)
        import shutil

        shutil.rmtree(os.path.join(str(MEMES_DIR), normal), ignore_errors=True)


def get_files(category):
    from astrbot_plugin_meme_manager.backend.models import get_emoji_by_category

    return get_emoji_by_category(category)


@pytest.mark.asyncio
async def test_clear_handler_rejects_external_paths(tmp_path, make_image):
    import asyncio
    from astrbot.api.web import PluginRequest, bind_request_context
    from astrbot_plugin_meme_manager.backend.dashboard_api import register_dashboard_apis

    outside = tmp_path / "outside_handler"
    outside.mkdir()
    sentinel = outside / "keep.png"
    sentinel.write_bytes(make_image())
    parent_sentinel = MEMES_DIR.parent / "outside_gallery.png"
    parent_sentinel.write_bytes(make_image())
    link = MEMES_DIR / "zz_clear_handler_link"
    link.symlink_to(outside, target_is_directory=True)
    handlers = {}

    class Context:
        def register_web_api(self, route, handler, methods, desc):
            handlers[route] = handler

    class Plugin:
        context = Context()
        _library_lock = asyncio.Lock()
        _sync_task = None

    class RawRequest:
        method = "POST"
        url = type("Url", (), {"path": "/x"})()
        headers = {}
        cookies = {}
        client = None
        query_params = type("Query", (), {"multi_items": lambda self: []})()

    register_dashboard_apis(Plugin())
    try:
        for category in ("..", link.name):
            req = PluginRequest(RawRequest(), plugin_name="meme_manager")
            with bind_request_context(req):
                response = await handlers["/meme_manager/categories/<category>/clear"](category)
            assert response.status_code == 400
            assert sentinel.exists() and parent_sentinel.exists()
    finally:
        link.unlink()
        parent_sentinel.unlink()


@pytest.mark.asyncio
async def test_overwrite_guard_covers_move_copy_and_queued_upload(
    fresh_category, make_image
):
    """真实 handler：覆盖任务在跑时拒绝移动/复制，排队上传也不能穿过。"""
    import asyncio
    import io
    import json

    from astrbot.api.web import (
        PluginMultiDict,
        PluginRequest,
        PluginUploadFile,
        bind_request_context,
    )
    from starlette.datastructures import UploadFile

    from astrbot_plugin_meme_manager.backend.dashboard_api import register_dashboard_apis

    src, target = fresh_category, f"{fresh_category}_target"
    add_emoji_to_category(src, make_image(), "source.png")
    handlers = {}

    class Context:
        def register_web_api(self, route, handler, methods, desc):
            handlers[route] = handler

    class Plugin:
        context = Context()
        img_sync = None
        _library_lock = asyncio.Lock()
        _sync_task = None
        _sync_info = {}

        async def reload_emotions(self):
            pass

    plugin = Plugin()
    register_dashboard_apis(plugin)

    class RawRequest:
        method = "POST"
        url = type("Url", (), {"path": "/x"})()
        headers = {}
        cookies = {}
        client = None
        query_params = type("Query", (), {"multi_items": lambda self: []})()

        def __init__(self, body=None):
            self.body = body or {}

        async def json(self):
            return self.body

    async def invoke(route, body=None, image=None, category=None):
        req = PluginRequest(RawRequest(body), plugin_name="meme_manager")
        if image is not None:
            req._form_cache = PluginMultiDict([])
            req._files_cache = PluginMultiDict(
                [("file", PluginUploadFile(UploadFile(file=io.BytesIO(image), filename="queued.png")))]
            )
        with bind_request_context(req):
            resp = await handlers[route](*([category] if category else []))
        return resp.status_code, json.loads(resp.body)

    plugin._sync_task = asyncio.get_running_loop().create_future()
    plugin._sync_info = {"direction": "overwrite_from_remote"}
    for op in ("move", "copy"):
        status, _ = await invoke(
            f"/meme_manager/images/{op}",
            {"items": [{"category": src, "name": "source.png"}], "target_category": target},
        )
        assert status == 409
        assert not (MEMES_DIR / target / "source.png").exists()
    plugin._sync_task.cancel()

    await plugin._library_lock.acquire()
    queued = asyncio.create_task(
        invoke(
            "/meme_manager/categories/<category>/images",
            category=target,
            image=make_image("PNG", color=(1, 2, 3)),
        )
    )
    try:
        for _ in range(100):
            if plugin._library_lock._waiters:
                break
            await asyncio.sleep(0.01)
        assert plugin._library_lock._waiters
        plugin._sync_task = asyncio.get_running_loop().create_future()
        plugin._sync_info = {"direction": "overwrite_to_remote"}
    finally:
        plugin._library_lock.release()
    status, _ = await queued
    assert status == 409
    assert not (MEMES_DIR / target / "queued.png").exists()
    plugin._sync_task.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["move", "copy"])
async def test_move_copy_handlers_reject_external_target(tmp_path, make_image, operation):
    """真实移动/复制 handler：目标分类是外部软链接时，文件不能写到图库外。"""
    import asyncio

    from astrbot_plugin_meme_manager.backend.dashboard_api import (
        register_dashboard_apis,
    )

    outside = tmp_path / "outside3"
    outside.mkdir()
    link = os.path.join(str(MEMES_DIR), "zz_link_target")
    os.symlink(str(outside), link)

    src = "zz_mc_src"
    add_emoji_to_category(src, make_image("PNG"), "m.png")

    handlers = {}

    class FakeContext:
        def register_web_api(self, route, handler, methods, desc):
            handlers[route] = handler

    class FakePlugin:
        category_manager = None
        img_sync = None
        _library_lock = asyncio.Lock()
        context = FakeContext()

        async def reload_emotions(self):
            pass

    try:
        register_dashboard_apis(FakePlugin())

        handler = handlers[f"/meme_manager/images/{operation}"]
        from astrbot.api.web import PluginRequest, bind_request_context

        class FakeRawRequest:
            method = "POST"
            url = type("U", (), {"path": "/api/v1/plugins/extensions/meme_manager/images/move"})()
            headers = {}
            cookies = {}
            client = None
            query_params = type("Q", (), {"multi_items": lambda self: []})()

            async def json(self):
                return {
                    "items": [{"category": src, "name": "m.png"}],
                    "target_category": "zz_link_target",
                }

        req = PluginRequest(FakeRawRequest(), plugin_name="meme_manager")
        with bind_request_context(req):
            resp = await handler()
        body = resp.body.decode()
        assert "moved" not in body or '"moved":[]' in body
        # 文件仍在源分类，图库外没有写入
        assert "m.png" in get_files(src)
        assert not (outside / "m.png").exists()
    finally:
        os.unlink(link)
        import shutil

        shutil.rmtree(os.path.join(str(MEMES_DIR), src), ignore_errors=True)
