"""格式与动画：五种格式识别、GIF 字节保留、统一选图规则、_convert_to_gif 多帧保护。"""

import os

from astrbot_plugin_meme_manager.backend.models import (
    IMAGE_EXTENSIONS,
    add_emoji_to_category,
    get_emoji_by_category,
)
from astrbot_plugin_meme_manager.main import list_category_meme_files


def test_all_formats_listed(fresh_category, make_image, memes_dir):
    # 内容决定扩展名：JPEG 内容统一落成 .jpg（后缀比较忽略大小写）
    cases = [
        ("PNG", "p.PNG", "p.png"),
        ("JPEG", "j.jpeg", "j.jpg"),
        ("JPEG", "j2.jpg", "j2.jpg"),
        ("GIF", "g.gif", "g.gif"),
        ("WEBP", "w.webp", "w.webp"),
    ]
    for i, (fmt, name, _saved) in enumerate(cases):
        add_emoji_to_category(
            fresh_category, make_image(fmt, color=(i * 30, i * 20, i * 10)), name
        )
    listed = get_emoji_by_category(fresh_category)
    assert sorted(listed) == sorted(saved for _, _, saved in cases)
    # 机器人选图路径用同一套规则
    robot_listed = list_category_meme_files(
        os.path.join(memes_dir, fresh_category)
    )
    assert sorted(robot_listed) == sorted(listed)


def test_animated_gif_bytes_preserved(fresh_category, make_image, memes_dir):
    content = make_image("GIF", frames=3)
    result = add_emoji_to_category(fresh_category, content, "动图.gif")
    assert result["is_animated"] is True
    stored = os.path.join(memes_dir, fresh_category, result["filename"])
    with open(stored, "rb") as f:
        assert f.read() == content  # 原始字节逐字节一致


def test_convert_to_gif_skips_animated(fresh_category, make_image, memes_dir):
    from astrbot_plugin_meme_manager.main import MemeSender

    path = os.path.join(memes_dir, fresh_category)
    os.makedirs(path, exist_ok=True)
    animated = os.path.join(path, "a.webp")
    with open(animated, "wb") as f:
        f.write(make_image("WEBP", frames=2))

    # 不实例化整个插件，只借用未绑定方法验证判断逻辑
    fake = type("Fake", (), {"convert_static_to_gif": True})()
    assert MemeSender._convert_to_gif(fake, animated) == animated

    static = os.path.join(path, "s.png")
    with open(static, "wb") as f:
        f.write(make_image("PNG"))
    converted = MemeSender._convert_to_gif(fake, static)
    assert converted != static
    assert converted.endswith(".gif")
    if os.path.exists(converted):
        os.remove(converted)


def test_convert_to_gif_disabled_passthrough(make_image, memes_dir, fresh_category):
    from astrbot_plugin_meme_manager.main import MemeSender

    fake = type("Fake", (), {"convert_static_to_gif": False})()
    assert MemeSender._convert_to_gif(fake, "/any/path.png") == "/any/path.png"


def test_unsupported_format_rejected(fresh_category):
    # BMP 不在第一版支持范围内
    import io

    from PIL import Image

    from astrbot_plugin_meme_manager.backend.models import (
        InvalidImageError,
    )
    import pytest

    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, format="BMP")
    with pytest.raises(InvalidImageError):
        add_emoji_to_category(fresh_category, buf.getvalue(), "x.bmp")
