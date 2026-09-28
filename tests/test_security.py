"""边界与路径安全：越界、软链接逃逸、非法名称。"""

import os

import pytest

from astrbot_plugin_meme_manager.backend.models import (
    InvalidCategoryError,
    InvalidFilenameError,
    delete_emoji_from_category,
    resolve_in_memes,
    sanitize_filename,
    validate_category_name,
    validate_filename,
)
from astrbot_plugin_meme_manager.config import MEMES_DIR


def test_path_traversal_rejected():
    for bad in ("..", "../x", "a/b", "a\\b", "/abs/path", "", ".hidden", "a?b", "a#b", "a%b"):
        with pytest.raises(InvalidCategoryError):
            validate_category_name(bad)


def test_filename_traversal_rejected():
    for bad in ("../x.png", "a/b.png", "..", "", ".png"):
        with pytest.raises(InvalidFilenameError):
            validate_filename(bad)


def test_resolve_stays_inside_memes():
    with pytest.raises(InvalidCategoryError):
        resolve_in_memes("..", "cmd_config.json")
    ok = resolve_in_memes("see", "x.png")
    assert str(ok).startswith(str(MEMES_DIR))


def test_symlink_escape_rejected(tmp_path):
    # 在图库里放一个指向外部的软链接分类目录，解析后越界必须拒绝
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(b"png")
    link = os.path.join(MEMES_DIR, "zz_symlink")
    os.symlink(str(outside), link)
    try:
        with pytest.raises(InvalidCategoryError):
            resolve_in_memes("zz_symlink", "secret.png")
        # 删除也不能顺着软链接删到外部文件
        assert not delete_emoji_from_category("zz_symlink", "secret.png")
        assert (outside / "secret.png").exists()
    finally:
        os.unlink(link)


def test_sanitize_filename_rules():
    assert sanitize_filename("可爱.jpg") == "可爱.jpg"
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("a\\b.png") == "ab.png"
    assert sanitize_filename("") is None
    assert sanitize_filename("...") is None
    assert sanitize_filename("   ") is None
