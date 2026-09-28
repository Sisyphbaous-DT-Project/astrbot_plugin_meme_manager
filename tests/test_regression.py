"""旧行为回归：真实调用 MemeSender 的 resp / on_decorating_result / _send_memes_streaming。

不复制模拟逻辑，直接构造最小 fake 依赖调用真实方法。
"""

import os
import types

import pytest

from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain, ResultContentType

from astrbot_plugin_meme_manager.config import MEMES_DIR
from astrbot_plugin_meme_manager.main import MemeSender


class FakeEvent:
    def __init__(self, result=None, text=""):
        self._result = result
        self._text = text
        self.extras = {}
        self.unified_msg_origin = "test:friend:1"
        self.sent = []

    def get_result(self):
        return self._result

    def get_message_str(self):
        return self._text

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def get_platform_name(self):
        return "test"

    async def send(self, chain):
        self.sent.append(chain)


class FakeContext:
    def __init__(self):
        self.sent = []
        self.provider_manager = types.SimpleNamespace(personas=[{"prompt": "人格"}])

    async def send_message(self, umo, chain):
        self.sent.append(chain)


def make_sender(tmp_meme=True, **overrides):
    """绕过 __init__ 构造最小可用的 MemeSender。"""
    sender = MemeSender.__new__(MemeSender)
    sender.config = {
        "enable_alternative_markup": True,
        "enable_repeated_emotion_detection": False,
        "enable_loose_emotion_matching": False,
        "high_confidence_emotions": [],
    }
    sender.remove_invalid_alternative_markup = False
    sender.category_mapping = {"happy": "开心", "sad": "难过"}
    sender.max_emotions_per_message = 2
    sender.emotion_llm_enabled = False
    sender.found_emotions = []
    sender.emotions_probability = 100
    sender.send_meme_when_empty = True
    sender.always_send_meme_when_empty = True
    sender.enable_mixed_message = True
    sender.mixed_message_probability = 100
    sender.convert_static_to_gif = False
    sender.streaming_compatibility = True
    sender.content_cleanup_rule = "&&[a-zA-Z]*&&"
    sender.context = FakeContext()
    for k, v in overrides.items():
        setattr(sender, k, v)
    return sender


@pytest.fixture()
def happy_dir(make_image):
    path = os.path.join(str(MEMES_DIR), "happy")
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "t.png"), "wb") as f:
        f.write(make_image("PNG"))
    yield path


def make_response(text):
    return types.SimpleNamespace(completion_text=text, result_chain=None)


# ---------- resp：Anthropic 纯表情修复 ----------

@pytest.mark.asyncio
async def test_pure_emoji_reply_gets_empty_plain_chain():
    """f2821ef 修复：正文清空后 result_chain 为空时补空 Plain，让流程走完。"""
    sender = make_sender()
    event = FakeEvent()
    response = make_response("&&happy&&")
    await sender.resp(event, response)

    assert sender.found_emotions == ["happy"]
    assert response.completion_text == ""
    assert response.result_chain is not None  # 修复生效：补了空 Plain 链
    assert event.get_extra("meme_manager_empty_meme_reply") is True


@pytest.mark.asyncio
async def test_empty_or_thinking_only_no_meme():
    """纯空 / 仅思考内容不凭空加图。"""
    sender = make_sender()
    event = FakeEvent()
    response = make_response("<thinking>在想事情</thinking>")
    await sender.resp(event, response)
    assert sender.found_emotions == []
    assert response.result_chain is None  # 没有触发补链
    assert event.get_extra("meme_manager_empty_meme_reply") is False


@pytest.mark.asyncio
async def test_normal_text_with_tag_keeps_text():
    sender = make_sender()
    event = FakeEvent()
    response = make_response("你好呀 &&happy&& 世界")
    await sender.resp(event, response)
    assert sender.found_emotions == ["happy"]
    assert "你好呀" in response.completion_text
    assert "&&" not in response.completion_text
    assert event.get_extra("meme_manager_empty_meme_reply") is False


# ---------- on_decorating_result：非流式选图 ----------

@pytest.mark.asyncio
async def test_decorating_adds_image_with_probability(happy_dir, monkeypatch):
    sender = make_sender()
    sender.found_emotions = ["happy"]
    monkeypatch.setattr("random.randint", lambda a, b: 1)  # 必中概率

    result = types.SimpleNamespace(
        result_content_type=None, chain=[Plain("正文内容")]
    )
    event = FakeEvent(result=result)
    await sender.on_decorating_result(event)

    chain = result.chain
    has_image = any(
        not isinstance(c, Plain) for c in chain
    )
    assert has_image  # 图片被混入消息链
    assert sender.found_emotions == []  # 处理后清空


@pytest.mark.asyncio
async def test_decorating_respects_probability_zero(happy_dir, monkeypatch):
    sender = make_sender()
    sender.found_emotions = ["happy"]
    sender.emotions_probability = 1
    monkeypatch.setattr("random.randint", lambda a, b: 100)  # 必不中

    result = types.SimpleNamespace(
        result_content_type=None, chain=[Plain("正文内容")]
    )
    event = FakeEvent(result=result)
    await sender.on_decorating_result(event)

    assert all(isinstance(c, Plain) for c in result.chain)  # 没有图片
    assert event.get_extra("meme_manager_pending_images") is None


# ---------- _send_memes_streaming：流式选图 ----------

@pytest.mark.asyncio
async def test_streaming_sends_image_once(happy_dir, monkeypatch):
    sender = make_sender()
    sender.found_emotions = ["happy"]
    monkeypatch.setattr("random.randint", lambda a, b: 1)

    event = FakeEvent()
    await sender._send_memes_streaming(event, force_send=False)

    assert len(sender.context.sent) == 1  # 只发一次
    assert sender.found_emotions == []


@pytest.mark.asyncio
async def test_streaming_skips_on_probability(happy_dir, monkeypatch):
    sender = make_sender()
    sender.found_emotions = ["happy"]
    sender.emotions_probability = 1
    monkeypatch.setattr("random.randint", lambda a, b: 100)

    event = FakeEvent()
    await sender._send_memes_streaming(event, force_send=False)
    assert sender.context.sent == []


@pytest.mark.asyncio
async def test_streaming_force_send_for_empty_reply(happy_dir, monkeypatch):
    sender = make_sender()
    sender.found_emotions = ["happy"]
    sender.emotions_probability = 1
    monkeypatch.setattr("random.randint", lambda a, b: 100)

    event = FakeEvent()
    await sender._send_memes_streaming(event, force_send=True)  # 纯表情回复必发
    assert len(sender.context.sent) == 1
