"""Tests for cache-friendly prompt construction."""

from __future__ import annotations

from datetime import datetime as real_datetime
from importlib.resources import files as pkg_files
from pathlib import Path
import datetime as datetime_module

from nanobot.agent.context import ContextBuilder
from nanobot.config.schema import Config


class _FakeDatetime(real_datetime):
    current = real_datetime(2026, 2, 24, 13, 59)

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls.current


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    return workspace


def test_bootstrap_files_are_backed_by_templates() -> None:
    template_dir = pkg_files("nanobot") / "templates"

    for filename in ContextBuilder.BOOTSTRAP_FILES:
        assert (template_dir / filename).is_file(), f"missing bootstrap template: {filename}"


def test_system_prompt_stays_stable_when_clock_changes(tmp_path, monkeypatch) -> None:
    """System prompt should not change just because wall clock minute changes."""
    monkeypatch.setattr(datetime_module, "datetime", _FakeDatetime)

    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    _FakeDatetime.current = real_datetime(2026, 2, 24, 13, 59)
    prompt1 = builder.build_system_prompt()

    _FakeDatetime.current = real_datetime(2026, 2, 24, 14, 0)
    prompt2 = builder.build_system_prompt()

    assert prompt1 == prompt2


def test_runtime_context_is_separate_untrusted_user_message(tmp_path) -> None:
    """Runtime metadata should be merged with the user message."""
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    messages = builder.build_messages(
        history=[],
        current_message="Return exactly: OK",
        channel="cli",
        chat_id="direct",
    )

    assert messages[0]["role"] == "system"
    assert "## Current Session" not in messages[0]["content"]

    # Runtime context is now merged with user message into a single message
    assert messages[-1]["role"] == "user"
    user_content = messages[-1]["content"]
    assert isinstance(user_content, str)
    assert ContextBuilder._RUNTIME_CONTEXT_TAG in user_content
    assert "Current Time:" in user_content
    assert "Channel:" not in user_content
    assert "Chat ID:" not in user_content
    assert "Return exactly: OK" in user_content


def test_runtime_context_can_be_disabled_for_current_turn(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    messages = builder.build_messages(
        history=[],
        current_message="Return exactly: OK",
        channel="weixin",
        chat_id="acct|user",
        include_runtime_time=False,
    )

    user_content = messages[-1]["content"]
    assert isinstance(user_content, str)
    assert ContextBuilder._RUNTIME_CONTEXT_TAG not in user_content
    assert "Current Time:" not in user_content
    assert user_content == "Return exactly: OK"


def test_system_prompt_accepts_extra_sections(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    prompt = builder.build_system_prompt(extra_sections=["# Semantic Recall\n\n- User likes jasmine tea"])

    assert "# Semantic Recall" in prompt
    assert "User likes jasmine tea" in prompt


def test_system_prompt_omits_markdown_memory_when_disabled(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    (workspace / "memory").mkdir()
    (workspace / "memory" / "MEMORY.md").write_text("secret legacy fact", encoding="utf-8")
    config = Config.model_validate(
        {
            "memory": {
                "markdown": {
                    "enabled": False,
                },
                "semantic": {
                    "enabled": True,
                },
            }
        }
    )

    builder = ContextBuilder(workspace, memory_config=config.memory)
    prompt = builder.build_system_prompt()

    assert "secret legacy fact" not in prompt
    assert "Markdown memory files are disabled" in prompt
