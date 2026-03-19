# tests/providers/test_callbacks.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path


def test_callback_module_exists():
    from nanobot.providers.callbacks import ConversationCallback
    assert ConversationCallback is not None


def test_callback_inherits_custom_logger():
    """Verify callback follows LiteLLM's CustomLogger pattern."""
    from nanobot.providers.callbacks import ConversationCallback
    from litellm.integrations.custom_logger import CustomLogger

    cb = ConversationCallback()
    assert isinstance(cb, CustomLogger)
    assert hasattr(cb, "async_log_success_event")
    assert hasattr(cb, "async_log_failure_event")


@pytest.mark.asyncio
async def test_async_log_success_event_extracts_full_messages():
    """Verify callback captures full message history from kwargs["messages"]."""
    from nanobot.providers.callbacks import ConversationCallback

    cb = ConversationCallback()

    # Simulate a subagent conversation with tool calls
    kwargs = {
        "model": "anthropic/claude-opus-4-5",
        "messages": [
            {"role": "system", "content": "You are a subagent..."},
            {"role": "user", "content": "Read test.txt and summarize it"},
            {"role": "assistant", "content": "I'll read the file.", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "test.txt"}'}}
            ]},
            {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "Hello World"},
            {"role": "assistant", "content": "The file contains: Hello World"},
        ],
        "litellm_params": {
            "metadata": {
                "session_key": "subagent:abc12345",
                "agent_type": "subagent",
                "parent_session": "cli:direct",
                "task_id": "abc12345",
            }
        },
        "response_cost": 0.0025,
        "cache_hit": False,
    }

    response_obj = MagicMock()
    response_obj.choices = [MagicMock()]
    response_obj.choices[0].message.content = "The file contains: Hello World"
    response_obj.choices[0].finish_reason = "stop"
    response_obj.usage = MagicMock()
    response_obj.usage.prompt_tokens = 150
    response_obj.usage.completion_tokens = 20
    response_obj.usage.total_tokens = 170

    # Should not raise
    await cb.async_log_success_event(kwargs, response_obj, 0.0, 0.5)


@pytest.mark.asyncio
async def test_callback_writes_to_jsonl(tmp_path):
    """Verify callback writes trace to JSONL file."""
    from nanobot.providers.callbacks import ConversationCallback

    jsonl_path = tmp_path / "traces.jsonl"
    cb = ConversationCallback(jsonl_path=jsonl_path)

    kwargs = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hi"}],
        "litellm_params": {"metadata": {"session_key": "test:123"}},
        "response_cost": 0.001,
    }

    response_obj = MagicMock()
    response_obj.choices = [MagicMock()]
    response_obj.choices[0].message.content = "hello"
    response_obj.choices[0].finish_reason = "stop"
    response_obj.usage = MagicMock()
    response_obj.usage.prompt_tokens = 5
    response_obj.usage.completion_tokens = 5
    response_obj.usage.total_tokens = 10

    await cb.async_log_success_event(kwargs, response_obj, 0.0, 0.1)

    # Verify file was written
    assert jsonl_path.exists()
    import json
    with open(jsonl_path) as f:
        entry = json.loads(f.readline())
    assert entry["model"] == "test-model"
    assert len(entry["messages"]) == 1
    assert entry["metadata"]["session_key"] == "test:123"
