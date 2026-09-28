"""同步互斥：短锁登记立即返回、轮询只读快照、下载完成后刷新。"""

import asyncio

import pytest

from astrbot_plugin_meme_manager.main import MemeSender


class FakeSyncProcess:
    """语义对齐真实 multiprocessing.Process：is_alive/join/terminate。"""

    def __init__(self, alive=True, exitcode=0):
        self._alive = alive
        self.exitcode = exitcode
        self.terminated = False

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        self._alive = False

    def terminate(self):
        self.terminated = True
        self._alive = False


class FakeImgSync:
    """语义对齐真实 ImageSync：_start_sync_process 只返回进程，不登记。

    （真实实现不设置 self.sync_process，登记是插件 _run_image_sync 的职责。
    之前的替身多做了这一步，导致漏测——已修正。）
    """

    def __init__(self, status=None, exitcode=0):
        self.sync_process = None
        self.status = status or {}
        self.exitcode = exitcode
        self.started_tasks = []
        self.stopped = False

    def check_status(self):
        return self.status

    def _start_sync_process(self, task):
        self.started_tasks.append(task)
        return FakeSyncProcess(alive=True, exitcode=self.exitcode)

    def stop_sync(self):
        self.stopped = True
        if self.sync_process and self.sync_process.is_alive():
            self.sync_process.terminate()


class FakeCategoryManager:
    def __init__(self):
        self.sync_calls = 0

    def sync_with_filesystem(self):
        self.sync_calls += 1
        return True


def make_sender(img_sync):
    sender = MemeSender.__new__(MemeSender)
    sender.img_sync = img_sync
    sender._sync_lock = asyncio.Lock()
    sender._library_lock = asyncio.Lock()
    sender._sync_task = None
    sender._sync_start_task = None
    sender._sync_info = {}
    sender._sync_last_result = None
    sender.category_manager = FakeCategoryManager()
    sender.reload_calls = 0

    async def fake_reload():
        sender.reload_calls += 1

    sender.reload_emotions = fake_reload
    return sender


@pytest.mark.asyncio
async def test_busy_rejected_immediately_without_stopping_old():
    img_sync = FakeImgSync(status={"to_upload": [{"filename": "a"}]})
    sender = make_sender(img_sync)
    # 已有进程在跑
    img_sync.sync_process = FakeSyncProcess(alive=True)

    ok, message = await sender._start_image_sync("upload")
    assert not ok
    assert "进行中" in message
    # 旧进程没有被停止
    assert not img_sync.sync_process.terminated


@pytest.mark.asyncio
async def test_second_start_does_not_queue():
    img_sync = FakeImgSync(status={"to_upload": [{"filename": "a"}]})
    sender = make_sender(img_sync)

    ok1, _ = await sender._start_image_sync("upload")
    assert ok1
    # 任务登记后立即返回；第二次调用应立即拿到忙碌，而不是排队等第一个完成
    ok2, msg2 = await asyncio.wait_for(sender._start_image_sync("upload"), timeout=1)
    assert not ok2
    assert "进行中" in msg2
    await sender._sync_task
    assert sender._sync_last_result["success"] is True


@pytest.mark.asyncio
async def test_process_registered_by_plugin():
    """插件负责把子进程登记到 img_sync.sync_process（真实方法不登记）。"""
    img_sync = FakeImgSync(status={"to_upload": [{"filename": "a"}]})
    sender = make_sender(img_sync)
    ok, _ = await sender._start_image_sync("upload")
    assert ok
    await sender._sync_task
    assert img_sync.sync_process is not None  # 插件登记的
    assert img_sync.sync_process.exitcode == 0


@pytest.mark.asyncio
async def test_terminate_stops_running_sync():
    """同步进行中重载/卸载插件：后台任务被取消，子进程被停止。"""
    import threading

    img_sync = FakeImgSync(status={"to_upload": [{"filename": "a"}]})
    sender = make_sender(img_sync)

    real_start = img_sync._start_sync_process

    def hanging_start(task):
        proc = real_start(task)
        stop_event = threading.Event()
        original_terminate = proc.terminate

        def join(timeout=None):
            stop_event.wait(timeout=10)  # 长同步：直到 terminate 才返回

        def terminate():
            stop_event.set()
            original_terminate()

        proc.join = join
        proc.terminate = terminate
        return proc

    img_sync._start_sync_process = hanging_start

    ok, _ = await sender._start_image_sync("upload")
    assert ok
    await asyncio.sleep(0.2)  # 让后台任务进入 join 等待
    assert img_sync.sync_process is not None  # 已登记

    sender.context = type("C", (), {})()
    sender.context.provider_manager = type(
        "PM", (), {"personas": [{"prompt": "x"}]}
    )()
    sender.persona_backup = [{"prompt": "x"}]
    await asyncio.wait_for(sender.terminate(), timeout=8)
    assert img_sync.stopped  # stop_sync 被调用，进程被终止
    assert not img_sync.sync_process.is_alive()


@pytest.mark.asyncio
async def test_poll_is_readonly_and_result_survives():
    """复现旧坑：轮询若清空 sync_process，等待方会把成功误判失败。"""
    img_sync = FakeImgSync(status={"to_upload": [{"filename": "a"}]})
    sender = make_sender(img_sync)

    ok, _ = await sender._start_image_sync("upload")
    assert ok
    await sender._sync_task

    # 模拟 sync/process 轮询：只读快照，不清空
    snapshot_running = bool(sender._sync_task and not sender._sync_task.done())
    last = sender._sync_last_result
    assert snapshot_running is False
    assert last["success"] is True
    # 轮询后再读结果仍然正确（没有被清掉）
    assert sender._sync_last_result["success"] is True
    assert img_sync.sync_process is not None  # 未被轮询清空


@pytest.mark.asyncio
async def test_download_completion_refreshes_runtime():
    img_sync = FakeImgSync(status={"to_download": [{"filename": "b"}]})
    sender = make_sender(img_sync)

    ok, _ = await sender._start_image_sync("download")
    assert ok
    await sender._sync_task
    assert sender._sync_last_result["success"] is True
    # 下载类完成后刷新分类与提示词（在后台任务收尾，不依赖网页）
    assert sender.category_manager.sync_calls == 1
    assert sender.reload_calls == 1


@pytest.mark.asyncio
async def test_partial_download_still_refreshes_runtime():
    img_sync = FakeImgSync(
        status={"to_download": [{"filename": "a"}, {"filename": "b"}]},
        exitcode=1,
    )
    sender = make_sender(img_sync)
    ok, _ = await sender._start_image_sync("download")
    assert ok
    await sender._sync_task
    assert sender._sync_last_result["success"] is False
    assert sender.category_manager.sync_calls == 1
    assert sender.reload_calls == 1


@pytest.mark.asyncio
async def test_overwrite_registration_waits_for_active_library_write():
    img_sync = FakeImgSync(status={"to_delete_local": [{"filename": "a"}]})
    sender = make_sender(img_sync)
    await sender._library_lock.acquire()
    try:
        start = asyncio.create_task(sender._start_image_sync("overwrite_from_remote"))
        await asyncio.sleep(0)
        assert not start.done()
        assert sender._sync_task is None
    finally:
        sender._library_lock.release()
    ok, _ = await start
    assert ok
    await sender._sync_task


@pytest.mark.asyncio
async def test_terminate_during_process_start_stops_late_process():
    """启动线程刚好被重载打断，线程稍后返回时仍要停掉子进程。"""
    import threading

    started = threading.Event()
    release = threading.Event()
    processes = []
    img_sync = FakeImgSync(status={"to_upload": [{"filename": "a"}]})

    def slow_start(task):
        started.set()
        release.wait(timeout=5)
        proc = FakeSyncProcess(alive=True)
        processes.append(proc)
        return proc

    img_sync._start_sync_process = slow_start
    sender = make_sender(img_sync)
    sender.context = type("C", (), {})()
    sender.context.provider_manager = type(
        "PM", (), {"personas": [{"prompt": "x"}]}
    )()
    sender.persona_backup = [{"prompt": "x"}]
    try:
        ok, _ = await sender._start_image_sync("upload")
        assert ok
        assert await asyncio.to_thread(started.wait, 5)
        terminating = asyncio.create_task(sender.terminate())
        await asyncio.sleep(0.02)
    finally:
        release.set()
    await asyncio.wait_for(terminating, timeout=6)
    for _ in range(100):
        if processes and processes[0].terminated:
            break
        await asyncio.sleep(0.01)
    assert processes and processes[0].terminated
    assert not processes[0].is_alive()
    assert img_sync.sync_process is None


@pytest.mark.asyncio
async def test_terminate_restores_persona_after_download_cleanup():
    import threading

    joining = threading.Event()
    stopped = threading.Event()
    proc = FakeSyncProcess(alive=True)

    def join(timeout=None):
        joining.set()
        stopped.wait(timeout=5)

    def terminate():
        proc._alive = False
        stopped.set()

    proc.join = join
    proc.terminate = terminate
    img_sync = FakeImgSync(status={"to_download": [{"filename": "a"}]})
    img_sync._start_sync_process = lambda task: proc
    sender = make_sender(img_sync)
    personas = [{"prompt": "插件提示词"}]
    sender.context = type(
        "Context", (), {"provider_manager": type("PM", (), {"personas": personas})()}
    )()
    sender.persona_backup = [{"prompt": "原人格"}]

    async def reload():
        personas[0]["prompt"] = "收尾重新注入的提示词"

    sender.reload_emotions = reload
    await sender._start_image_sync("download")
    assert await asyncio.to_thread(joining.wait, 5)
    await sender.terminate()
    assert personas[0]["prompt"] == "原人格"
    assert not proc.is_alive()


@pytest.mark.asyncio
async def test_upload_does_not_refresh_runtime():
    img_sync = FakeImgSync(status={"to_upload": [{"filename": "a"}]})
    sender = make_sender(img_sync)
    ok, _ = await sender._start_image_sync("upload")
    assert ok
    await sender._sync_task
    assert sender.reload_calls == 0


@pytest.mark.asyncio
async def test_nothing_to_sync_is_success_without_process():
    img_sync = FakeImgSync(status={"to_upload": []})
    sender = make_sender(img_sync)
    ok, _ = await sender._start_image_sync("upload")
    assert ok
    await sender._sync_task
    assert sender._sync_last_result["success"] is True
    assert img_sync.started_tasks == []  # 没有起子进程
