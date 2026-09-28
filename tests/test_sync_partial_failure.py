"""S01：图床单项失败必须让整体返回失败（区分完全成功与部分失败）。"""

import os

from astrbot_plugin_meme_manager.image_host.core.sync_manager import SyncManager


class FakeHost:
    """假图床：远端为空；上传按文件名决定成功/失败。"""

    config = {"provider": "fake"}

    def __init__(self, fail_names=()):
        self.fail_names = set(fail_names)

    def get_image_list(self):
        return []

    def upload_image(self, file_path):
        if os.path.basename(str(file_path)) in self.fail_names:
            raise ConnectionError("模拟网络失败")
        return {"url": "https://example.com/x"}

    def download_image(self, image, save_path):
        return True

    def delete_image(self, file_id):
        return True


def _setup_local(tmp_path):
    memes = tmp_path / "memes"
    (memes / "cat1").mkdir(parents=True)
    for name in ("a.png", "b.png"):
        (memes / "cat1" / name).write_bytes(b"png-" + name.encode())
    return memes


def test_partial_upload_failure_returns_false(tmp_path):
    memes = _setup_local(tmp_path)
    host = FakeHost(fail_names={"b.png"})
    mgr = SyncManager(image_host=host, local_dir=memes, upload_tracker=None)
    assert mgr.sync_to_remote() is False  # 1 成功 1 失败 → 整体 False


def test_all_upload_success_returns_true(tmp_path):
    memes = _setup_local(tmp_path)
    host = FakeHost()
    mgr = SyncManager(image_host=host, local_dir=memes, upload_tracker=None)
    assert mgr.sync_to_remote() is True


def test_remote_delete_false_marks_overwrite_failed(tmp_path):
    class FailedDeleteHost(FakeHost):
        def get_image_list(self):
            return [
                {"id": "remote/missing.png", "filename": "missing.png", "category": "remote"}
            ]

        def delete_image(self, file_id):
            return False

    memes = tmp_path / "memes"
    memes.mkdir()
    mgr = SyncManager(image_host=FailedDeleteHost(), local_dir=memes)
    assert mgr.overwrite_to_remote() is False
