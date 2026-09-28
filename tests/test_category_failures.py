"""失败恢复：save_json 返回 False 时的回滚、目录改名撤回、原子写保护原文件。"""

import json
import os

from astrbot_plugin_meme_manager.backend.category_manager import CategoryManager
from astrbot_plugin_meme_manager import utils
from astrbot_plugin_meme_manager.config import MEMES_DATA_PATH, MEMES_DIR


def test_update_description_save_false_keeps_memory(monkeypatch):
    mgr = CategoryManager()
    before = dict(mgr.descriptions)
    monkeypatch.setattr(
        "astrbot_plugin_meme_manager.backend.category_manager.save_json_atomic",
        lambda data, path: False,
    )
    assert not mgr.update_description("zz_test_desc", "不会保存")
    assert mgr.descriptions == before
    assert "zz_test_desc" not in json.load(open(MEMES_DATA_PATH))


def test_rename_save_failure_rolls_back_directory(monkeypatch, tmp_path):
    mgr = CategoryManager()
    old = "zz_rename_old"
    new = "zz_rename_new"
    mgr.update_description(old, "旧分类")
    os.makedirs(os.path.join(MEMES_DIR, old), exist_ok=True)

    monkeypatch.setattr(
        "astrbot_plugin_meme_manager.backend.category_manager.save_json_atomic",
        lambda data, path: False,
    )
    assert not mgr.rename_category(old, new)
    # 目录被改回旧名
    assert os.path.isdir(os.path.join(MEMES_DIR, old))
    assert not os.path.exists(os.path.join(MEMES_DIR, new))
    # 内存与描述文件一致
    assert old in mgr.descriptions
    assert new not in mgr.descriptions
    assert old in json.load(open(MEMES_DATA_PATH))

    # 清理
    monkeypatch.undo()
    mgr.delete_category(old)


def test_delete_category_save_failure_keeps_directory(monkeypatch):
    mgr = CategoryManager()
    name = "zz_del_keep"
    mgr.update_description(name, "要删的分类")
    os.makedirs(os.path.join(MEMES_DIR, name), exist_ok=True)

    monkeypatch.setattr(
        "astrbot_plugin_meme_manager.backend.category_manager.save_json_atomic",
        lambda data, path: False,
    )
    assert not mgr.delete_category(name)
    assert os.path.isdir(os.path.join(MEMES_DIR, name))
    assert name in mgr.descriptions

    monkeypatch.undo()
    assert mgr.delete_category(name)


def test_save_json_atomic_failure_keeps_original(tmp_path, monkeypatch):
    target = tmp_path / "desc.json"
    original = {"a": 1}
    target.write_text(json.dumps(original), encoding="utf-8")

    def bad_replace(src, dst):
        raise OSError("模拟替换失败")

    monkeypatch.setattr("astrbot_plugin_meme_manager.utils.os.replace", bad_replace)
    assert not utils.save_json_atomic({"a": 2}, str(target))
    monkeypatch.undo()
    # 原文件字节不变，临时文件已清理
    assert json.loads(target.read_text(encoding="utf-8")) == original
    assert [p.name for p in tmp_path.iterdir()] == ["desc.json"]


def test_sync_with_filesystem_false_rolls_back(monkeypatch):
    mgr = CategoryManager()
    name = "zz_sync_fs"
    os.makedirs(os.path.join(MEMES_DIR, name), exist_ok=True)
    before = dict(mgr.descriptions)

    monkeypatch.setattr(
        "astrbot_plugin_meme_manager.backend.category_manager.save_json_atomic",
        lambda data, path: False,
    )
    assert not mgr.sync_with_filesystem()
    assert mgr.descriptions == before

    monkeypatch.undo()
    import shutil

    shutil.rmtree(os.path.join(MEMES_DIR, name), ignore_errors=True)
