"""整理与分类：批量部分成功、冲突保留、清空/删除语义、运行时提示词刷新。"""

import os

import pytest

from astrbot_plugin_meme_manager.backend.category_manager import CategoryManager
from astrbot_plugin_meme_manager.backend.models import (
    add_emoji_to_category,
    batch_delete_emojis,
    batch_move_emojis,
    clear_category_emojis,
    get_emoji_by_category,
)


def test_batch_move_partial_success_conflict_kept(fresh_category, make_image, memes_dir):
    target = fresh_category + "_t"
    add_emoji_to_category(fresh_category, make_image("PNG", color=(1, 1, 1)), "x.png")
    add_emoji_to_category(fresh_category, make_image("PNG", color=(2, 2, 2)), "y.png")
    # 目标分类预置同名文件制造冲突
    add_emoji_to_category(target, make_image("PNG", color=(9, 9, 9)), "x.png")

    result = batch_move_emojis(fresh_category, ["x.png", "y.png", "ghost.png"], target)
    assert result["moved_files"] == ["y.png"]
    assert result["conflicting_files"] == ["x.png"]
    assert result["missing_files"] == ["ghost.png"]
    # 冲突项仍留在源分类，目标分类的同名文件未被覆盖
    assert "x.png" in get_emoji_by_category(fresh_category)
    import hashlib

    with open(os.path.join(memes_dir, target, "x.png"), "rb") as f:
        assert hashlib.sha256(f.read()).hexdigest() == hashlib.sha256(
            make_image("PNG", color=(9, 9, 9))
        ).hexdigest()

    import shutil

    shutil.rmtree(os.path.join(memes_dir, target), ignore_errors=True)


def test_clear_vs_delete_category(fresh_category, make_image, memes_dir, plugin):
    mgr = CategoryManager()
    add_emoji_to_category(fresh_category, make_image("PNG"), "one.png")
    mgr.update_description(fresh_category, "测试说明")

    # 清空：图片没了，目录与说明还在
    result = clear_category_emojis(fresh_category)
    assert result["deleted_files"] == ["one.png"]
    assert os.path.isdir(os.path.join(memes_dir, fresh_category))
    assert mgr.get_descriptions()[fresh_category] == "测试说明"

    # 删除分类：目录与说明都没了
    assert mgr.delete_category(fresh_category)
    assert not os.path.exists(os.path.join(memes_dir, fresh_category))
    assert fresh_category not in mgr.get_descriptions()


def test_rename_conflict_rejected(fresh_category, make_image, memes_dir):
    mgr = CategoryManager()
    other = fresh_category + "_o"
    mgr.update_description(fresh_category, "a")
    mgr.update_description(other, "b")
    assert not mgr.rename_category(fresh_category, other)
    # 原样保留
    assert mgr.get_descriptions()[fresh_category] == "a"
    assert mgr.get_descriptions()[other] == "b"


def test_prompt_refresh_uses_real_reload(fresh_category, plugin, monkeypatch):
    """分类变更后，真实的 _reload_personas 必须用新的 category_mapping 重建提示词。"""
    from astrbot_plugin_meme_manager.main import MemeSender

    mgr = CategoryManager()
    mgr.update_description(fresh_category, "测试用的新分类说明")

    personas = [{"prompt": "基础人格"}]
    fake_context = type("Ctx", (), {})()
    fake_context.provider_manager = type("PM", (), {"personas": personas})()

    sender = MemeSender.__new__(MemeSender)
    sender.context = fake_context
    sender.category_manager = mgr
    sender.emotion_llm_enabled = False
    sender.prompt_head = "HEAD:"
    sender.prompt_tail_1 = "|TAIL1:"
    sender.prompt_tail_2 = ":TAIL2"
    sender.max_emotions_per_message = 2
    sender.persona_backup = [{"prompt": "基础人格"}]

    sender._reload_personas()
    prompt = personas[0]["prompt"]
    assert "HEAD:" in prompt
    assert fresh_category in prompt
    assert "测试用的新分类说明" in prompt

    # 删除分类后提示词里不再出现
    mgr.delete_category(fresh_category)
    sender._reload_personas()
    assert fresh_category not in personas[0]["prompt"]
