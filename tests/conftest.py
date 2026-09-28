"""pytest 导入前隔离：在任何插件 import 之前，把 AstrBot 数据根指到临时目录。

config.py 在导入时就会迁移数据、创建目录，所以必须在导入插件模块之前设置
ASTRBOT_ROOT 环境变量。整个测试会话共用一个隔离根，各测试用独立分类名避免互踩。
"""

import os
import sys
import tempfile
from pathlib import Path

# 1) 先隔离 AstrBot 数据路径（导入插件前）
_TEST_ROOT = tempfile.mkdtemp(prefix="meme_test_root_")
os.environ["ASTRBOT_ROOT"] = _TEST_ROOT

# 2) 让 astrbot 包与插件包可导入
_ASTRBOT_SRC = os.environ.get("ASTRBOT_SOURCE_DIR", "/root/projects/tmp/AstrBot")
_PLUGIN_PARENT = str(Path(__file__).resolve().parents[2])
for p in (_ASTRBOT_SRC, _PLUGIN_PARENT):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest


@pytest.fixture(scope="session")
def test_root():
    return _TEST_ROOT


@pytest.fixture(scope="session")
def plugin():
    """会话级共享的插件包（路径常量已固定在隔离根下）。"""
    import astrbot_plugin_meme_manager  # noqa: F401
    from astrbot_plugin_meme_manager import config

    return config


@pytest.fixture()
def fresh_category(plugin):
    """每个测试一个全新分类名，用后即删。"""
    import shutil
    import uuid

    name = f"t_{uuid.uuid4().hex[:8]}"
    yield name
    shutil.rmtree(os.path.join(str(plugin.MEMES_DIR), name), ignore_errors=True)


@pytest.fixture()
def make_image():
    """用 Pillow 现造测试图片字节。"""

    def _make(fmt="PNG", size=(16, 12), color=(200, 30, 30), frames=1):
        import io

        from PIL import Image

        imgs = [
            Image.new("RGB", size, tuple(min(255, c + i * 40) for c in color))
            for i in range(frames)
        ]
        buf = io.BytesIO()
        if frames > 1:
            imgs[0].save(
                buf, format=fmt, save_all=True, append_images=imgs[1:], duration=100, loop=0
            )
        else:
            imgs[0].save(buf, format=fmt)
        return buf.getvalue()

    return _make


@pytest.fixture()
def memes_dir(plugin):
    return str(plugin.MEMES_DIR)
