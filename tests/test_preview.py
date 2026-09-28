"""预览：缩略图首帧 vs 原图完整字节、缓存失效、bridge 解包字段回归。"""

import base64
import io
import json
import os
import pytest

from PIL import Image

from pathlib import Path

from astrbot_plugin_meme_manager.backend.media import (
    make_thumbnail,
    read_original_b64,
)
from astrbot_plugin_meme_manager.backend.models import add_emoji_to_category


def test_thumbnail_is_first_frame_original_kept(fresh_category, make_image, memes_dir):
    content = make_image("GIF", frames=3)
    add_emoji_to_category(fresh_category, content, "anim.gif")
    path = Path(os.path.join(memes_dir, fresh_category, "anim.gif"))

    thumb = make_thumbnail(fresh_category, path)
    assert thumb is not None
    # 缩略图是 PNG 单帧
    with Image.open(io.BytesIO(thumb)) as img:
        assert img.format == "PNG"
        assert getattr(img, "n_frames", 1) == 1
        assert max(img.size) <= 256

    # 原图接口返回完整 GIF 字节
    mime, b64 = read_original_b64(path)
    assert mime == "image/gif"
    assert base64.b64decode(b64) == content


def test_thumbnail_cache_key_changes_with_file(fresh_category, make_image, memes_dir):
    add_emoji_to_category(fresh_category, make_image("PNG", color=(1, 2, 3)), "c.png")
    path = Path(os.path.join(memes_dir, fresh_category, "c.png"))
    thumb1 = make_thumbnail(fresh_category, path)

    # 覆盖写入不同内容（模拟改名/替换）：缓存键按 mtime+size 变化，不串图
    with open(path, "wb") as f:
        f.write(make_image("PNG", size=(64, 64), color=(250, 0, 0)))
    os.stat(path)
    thumb2 = make_thumbnail(fresh_category, path)
    assert thumb1 != thumb2


def test_repeated_original_read_reuses_validation(fresh_category, make_image, memes_dir, monkeypatch):
    from astrbot_plugin_meme_manager.backend import media

    content = make_image("GIF", frames=3)
    add_emoji_to_category(fresh_category, content, "repeat.gif")
    path = Path(memes_dir) / fresh_category / "repeat.gif"
    real_sniff = media.sniff_image
    calls = []

    def counting_sniff(raw):
        calls.append(len(raw))
        return real_sniff(raw)

    monkeypatch.setattr(media, "sniff_image", counting_sniff)
    assert base64.b64decode(read_original_b64(path)[1]) == content
    assert base64.b64decode(read_original_b64(path)[1]) == content
    assert calls == [len(content)]

    path.write_bytes(make_image("PNG", size=(31, 13)))
    assert read_original_b64(path)[0] == "image/png"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_bridge_unwrap_keeps_all_fields(fresh_category, make_image):
    """用与 bridge 相同的 `data?.data ?? data` 规则模拟解包，确认图片接口字段完整。

    若响应顶层误用 `data` 键承载图片内容，解包后 mime/width 等字段会丢失。
    """
    from astrbot.api.web import PluginRequest, bind_request_context
    from astrbot_plugin_meme_manager.backend.dashboard_api import register_dashboard_apis

    content = make_image("GIF", size=(60, 40), frames=3)
    add_emoji_to_category(fresh_category, content, "bridge.gif")
    handlers = {}

    class Context:
        def register_web_api(self, route, handler, methods, desc):
            handlers[route] = handler

    class Plugin:
        context = Context()

    class Query:
        def multi_items(self):
            return [("category", fresh_category), ("name", "bridge.gif")]

    class RawRequest:
        method = "GET"
        url = type("Url", (), {"path": "/x"})()
        headers = {}
        cookies = {}
        client = None
        query_params = Query()

    register_dashboard_apis(Plugin())
    req = PluginRequest(RawRequest(), plugin_name="meme_manager")
    with bind_request_context(req):
        response = await handlers["/meme_manager/image"]()
    assert response.status_code == 200
    payload = json.loads(response.body)
    # bridge: response.data?.data ?? response.data
    resolved = payload.get("data", payload)
    assert resolved["content"] == payload["content"]
    assert resolved["mime"] == "image/gif"
    assert resolved["is_animated"] is True
    assert resolved["width"] == 60
    assert base64.b64decode(resolved["content"]) == content
    # 顶层确实没有 data 键
    assert "data" not in payload
