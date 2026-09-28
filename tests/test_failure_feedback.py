"""失败反馈与回滚：上传后刷新失败的如实区分、新建分类失败回滚（R03/R04）。"""

import asyncio
import os

import pytest

from astrbot.api.web import PluginRequest, PluginUploadFile, bind_request_context
from astrbot_plugin_meme_manager.backend.category_manager import CategoryManager
from astrbot_plugin_meme_manager.backend.dashboard_api import register_dashboard_apis
from astrbot_plugin_meme_manager.config import MEMES_DIR
from astrbot_plugin_meme_manager.main import MemeSender


class FakeContext:
    def __init__(self):
        self.routes = {}

    def register_web_api(self, route, handler, methods, desc):
        self.routes[route] = handler


def make_plugin():
    ctx = FakeContext()
    plugin = type("P", (), {})()
    plugin.context = ctx
    plugin.category_manager = CategoryManager()
    plugin.img_sync = None
    plugin._library_lock = asyncio.Lock()
    plugin._sync_task = None
    plugin._sync_info = {}
    plugin._sync_last_result = None

    async def _noop_reload():
        pass

    plugin.reload_emotions = _noop_reload
    register_dashboard_apis(plugin)
    return plugin


class FakeRawRequest:
    method = "POST"
    url = type("U", (), {"path": "/x"})()
    headers = {}
    cookies = {}
    client = None
    query_params = type("Q", (), {"multi_items": lambda self: []})()

    def __init__(self, body=None):
        self._body = body or {}

    async def json(self):
        return self._body


def bind(body=None):
    req = PluginRequest(FakeRawRequest(body), plugin_name="meme_manager")
    return bind_request_context(req)


@pytest.mark.asyncio
async def test_reload_emotions_raises_on_sync_false(monkeypatch):
    """R03：sync_with_filesystem 返回 False 时 reload_emotions 必须抛错。"""
    sender = MemeSender.__new__(MemeSender)
    sender.category_manager = type(
        "CM", (), {"sync_with_filesystem": lambda self: False}
    )()
    sender._reload_personas = lambda: None
    with pytest.raises(RuntimeError):
        await sender.reload_emotions()


@pytest.mark.asyncio
async def test_upload_refresh_failure_is_flagged(monkeypatch, make_image):
    """R03：上传 handler 刷新失败时返回 refresh_ok=False，不谎报一切正常。"""
    plugin = make_plugin()

    async def bad_reload():
        raise RuntimeError("模拟刷新失败")

    plugin.reload_emotions = bad_reload

    handler = plugin.context.routes["/meme_manager/categories/<category>/images"]

    # 伪造 multipart 上传请求
    content = make_image("PNG")

    import io

    from starlette.datastructures import UploadFile as StarletteUploadFile

    real_upload = PluginUploadFile(
        StarletteUploadFile(file=io.BytesIO(content), filename="r03.png")
    )

    class UploadReq(FakeRawRequest):
        pass

    req = PluginRequest(UploadReq(), plugin_name="meme_manager")
    # 直接塞 form/files 缓存，绕过 multipart 解析
    from astrbot.api.web import PluginMultiDict

    req._form_cache = PluginMultiDict([])
    req._files_cache = PluginMultiDict([("file", real_upload)])
    with bind_request_context(req):
        resp = await handler("r03_cat")

    import json

    body = json.loads(resp.body.decode())
    assert body["outcome"] == "added"  # 文件确实保存了
    assert body["refresh_ok"] is False  # 但如实标注刷新失败
    assert "刷新失败" in body["message"]
    # 文件真实落盘
    assert "r03.png" in os.listdir(os.path.join(str(MEMES_DIR), "r03_cat"))


@pytest.mark.asyncio
async def test_create_category_rolls_back_on_save_failure(monkeypatch):
    """R04：说明保存失败时撤销本次新建的空目录；重试能正常创建。"""
    plugin = make_plugin()
    handler = plugin.context.routes["/meme_manager/categories"]
    name = "zz_create_rollback"
    dir_path = os.path.join(str(MEMES_DIR), name)

    monkeypatch.setattr(
        "astrbot_plugin_meme_manager.backend.category_manager.save_json_atomic",
        lambda data, path: False,
    )
    with bind({"name": name, "description": "x"}):
        resp = await handler()
    assert resp.status_code == 500
    assert not os.path.exists(dir_path)  # 目录被撤销
    assert name not in plugin.category_manager.get_descriptions()

    # 恢复正常后重试，应能成功（不是 409 已存在）
    monkeypatch.undo()
    with bind({"name": name, "description": "x"}):
        resp2 = await handler()
    assert json_loads(resp2)["created"] is True
    assert os.path.isdir(dir_path)

    # 清理
    plugin.category_manager.delete_category(name)


def json_loads(resp):
    import json

    return json.loads(resp.body.decode())
