"""文件落盘：中文名、同名冲突、重复、伪造、超限、临时文件清理。"""

import os

import pytest

from astrbot_plugin_meme_manager.backend.models import (
    MAX_UPLOAD_BYTES,
    DuplicateEmojiError,
    FileTooLargeError,
    InvalidCategoryError,
    InvalidImageError,
    add_emoji_to_category,
    get_emoji_by_category,
)


def test_chinese_filename_preserved(fresh_category, make_image, memes_dir):
    content = make_image("JPEG")
    result = add_emoji_to_category(fresh_category, content, "可爱.jpg")
    assert result["outcome"] == "added"
    assert result["filename"] == "可爱.jpg"
    assert os.path.isfile(os.path.join(memes_dir, fresh_category, "可爱.jpg"))
    assert "可爱.jpg" in get_emoji_by_category(fresh_category)


def test_chinese_filename_no_ext_loss(fresh_category, make_image):
    # 旧版 secure_filename 会把「可爱.jpg」清成无扩展名的「jpg」，本次必须保留
    result = add_emoji_to_category(fresh_category, make_image("PNG"), "可爱.jpg")
    # 扩展名以内容为准：PNG 内容 + .jpg 名字 → 保存为 .png
    assert result["filename"] == "可爱.png"
    assert result["mime_type"] == "image/png"


def test_same_name_different_content_renamed(fresh_category, make_image, memes_dir):
    add_emoji_to_category(fresh_category, make_image("PNG", color=(10, 10, 10)), "a.png")
    result = add_emoji_to_category(
        fresh_category, make_image("PNG", color=(200, 200, 200)), "a.png"
    )
    assert result["filename"] == "a_1.png"
    # 旧文件不能被覆盖
    first = os.path.join(memes_dir, fresh_category, "a.png")
    assert os.path.isfile(first)
    with open(first, "rb") as f:
        assert f.read() == make_image("PNG", color=(10, 10, 10))


def test_duplicate_same_content(fresh_category, make_image):
    content = make_image("GIF", frames=2)
    add_emoji_to_category(fresh_category, content, "b.gif")
    with pytest.raises(DuplicateEmojiError) as exc:
        add_emoji_to_category(fresh_category, content, "换个名字.gif")
    assert exc.value.existing_filename == "b.gif"


def test_empty_and_fake_rejected(fresh_category):
    with pytest.raises(InvalidImageError):
        add_emoji_to_category(fresh_category, b"", "empty.png")
    with pytest.raises(InvalidImageError):
        add_emoji_to_category(fresh_category, b"this is not an image", "fake.jpg")


def test_truncated_image_rejected(fresh_category, make_image):
    """R05：只剩头部的截断 PNG 不能当作上传成功（PIL 惰性读取只验头不够）。"""
    content = make_image("PNG")
    truncated = content[:41]
    with pytest.raises(InvalidImageError):
        add_emoji_to_category(fresh_category, truncated, "truncated.png")
    # 完整文件不受影响
    result = add_emoji_to_category(fresh_category, content, "full.png")
    assert result["outcome"] == "added"


def test_oversize_rejected(fresh_category):
    with pytest.raises(FileTooLargeError):
        add_emoji_to_category(
            fresh_category, b"x" * (MAX_UPLOAD_BYTES + 1), "big.png"
        )


def test_failure_leaves_no_temp_and_keeps_existing(fresh_category, make_image, memes_dir):
    add_emoji_to_category(fresh_category, make_image("PNG"), "keep.png")
    with pytest.raises(InvalidImageError):
        add_emoji_to_category(fresh_category, b"junk", "bad.png")
    files = os.listdir(os.path.join(memes_dir, fresh_category))
    assert files == ["keep.png"]  # 没有残留临时文件，已有文件未被误删


def test_invalid_category_rejected(fresh_category, make_image):
    for bad in ("../etc", "a/b", "a\\b", "", ".", "..", ".hidden", "a?b", "a#b", "a%b"):
        with pytest.raises(InvalidCategoryError):
            add_emoji_to_category(bad, make_image("PNG"), "x.png")


def test_default_name_for_extensionless(fresh_category, make_image):
    # 文件名清理后无主体时生成默认名，扩展名以真实格式为准
    result = add_emoji_to_category(fresh_category, make_image("GIF"), "...")
    assert result["filename"].endswith(".gif")
    assert result["filename"].startswith("image_")
