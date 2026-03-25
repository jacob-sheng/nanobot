from datetime import datetime, timedelta

from nanobot.agent.context import ContextBuilder
from nanobot.agent.loop import AgentLoop
from nanobot.session.manager import Session


def _mk_loop() -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop._TOOL_RESULT_MAX_CHARS = AgentLoop._TOOL_RESULT_MAX_CHARS
    loop._WEIXIN_RUNTIME_TIME_IDLE_SECONDS = AgentLoop._WEIXIN_RUNTIME_TIME_IDLE_SECONDS
    return loop


def test_weixin_runtime_time_is_skipped_for_new_session() -> None:
    loop = _mk_loop()
    session = Session(key="weixin:test")

    assert loop._should_include_runtime_time("weixin", session) is False


def test_weixin_runtime_time_is_skipped_within_idle_window() -> None:
    loop = _mk_loop()
    session = Session(key="weixin:test")
    session.messages.append(
        {
            "role": "assistant",
            "content": "recent",
            "timestamp": (datetime.now() - timedelta(minutes=5)).isoformat(),
        }
    )

    assert loop._should_include_runtime_time("weixin", session) is False


def test_weixin_runtime_time_is_included_after_idle_window() -> None:
    loop = _mk_loop()
    session = Session(key="weixin:test")
    session.messages.append(
        {
            "role": "assistant",
            "content": "older",
            "timestamp": (datetime.now() - timedelta(minutes=11)).isoformat(),
        }
    )

    assert loop._should_include_runtime_time("weixin", session) is True


def test_weixin_runtime_time_is_skipped_on_invalid_timestamp() -> None:
    loop = _mk_loop()
    session = Session(key="weixin:test")
    session.messages.append(
        {
            "role": "assistant",
            "content": "bad-ts",
            "timestamp": "not-a-timestamp",
        }
    )

    assert loop._should_include_runtime_time("weixin", session) is False


def test_non_weixin_runtime_time_stays_enabled() -> None:
    loop = _mk_loop()
    session = Session(key="telegram:test")

    assert loop._should_include_runtime_time("telegram", session) is True


def test_save_turn_skips_multimodal_user_when_only_runtime_context() -> None:
    loop = _mk_loop()
    session = Session(key="test:runtime-only")
    runtime = ContextBuilder._RUNTIME_CONTEXT_TAG + "\nCurrent Time: now (UTC)"

    loop._save_turn(
        session,
        [{"role": "user", "content": [{"type": "text", "text": runtime}]}],
        skip=0,
    )
    assert session.messages == []


def test_save_turn_keeps_image_placeholder_with_path_after_runtime_strip() -> None:
    loop = _mk_loop()
    session = Session(key="test:image")
    runtime = ContextBuilder._RUNTIME_CONTEXT_TAG + "\nCurrent Time: now (UTC)"

    loop._save_turn(
        session,
        [{
            "role": "user",
            "content": [
                {"type": "text", "text": runtime},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}, "_meta": {"path": "/media/feishu/photo.jpg"}},
            ],
        }],
        skip=0,
    )
    assert session.messages[0]["content"] == [{"type": "text", "text": "[image: /media/feishu/photo.jpg]"}]


def test_save_turn_keeps_image_placeholder_without_meta() -> None:
    loop = _mk_loop()
    session = Session(key="test:image-no-meta")
    runtime = ContextBuilder._RUNTIME_CONTEXT_TAG + "\nCurrent Time: now (UTC)"

    loop._save_turn(
        session,
        [{
            "role": "user",
            "content": [
                {"type": "text", "text": runtime},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
        }],
        skip=0,
    )
    assert session.messages[0]["content"] == [{"type": "text", "text": "[image]"}]


def test_save_turn_keeps_tool_results_under_16k() -> None:
    loop = _mk_loop()
    session = Session(key="test:tool-result")
    content = "x" * 12_000

    loop._save_turn(
        session,
        [{"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": content}],
        skip=0,
    )

    assert session.messages[0]["content"] == content
