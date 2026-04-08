"""Tests for the Dream class — two-phase memory consolidation via AgentRunner."""

import json
import pytest

from unittest.mock import AsyncMock, MagicMock

from nanobot.agent.memory import Dream, MemoryStore
from nanobot.agent.runner import AgentRunResult
from nanobot.config.schema import MarkdownMemoryConfig


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path)
    s.write_soul("# Soul\n- Helpful")
    s.write_user("# User\n- Developer")
    s.write_memory("# Memory\n- Project X active")
    return s


@pytest.fixture
def mock_provider():
    p = MagicMock()
    p.chat_with_retry = AsyncMock()
    return p


@pytest.fixture
def mock_runner():
    return MagicMock()


@pytest.fixture
def dream(store, mock_provider, mock_runner):
    d = Dream(store=store, provider=mock_provider, model="test-model", max_batch_size=5)
    d._runner = mock_runner
    return d


def _make_run_result(
    stop_reason="completed",
    final_content=None,
    tool_events=None,
    usage=None,
):
    return AgentRunResult(
        final_content=final_content or stop_reason,
        stop_reason=stop_reason,
        messages=[],
        tools_used=[],
        usage={},
        tool_events=tool_events or [],
    )


class _FakeSemanticMemory:
    def __init__(self, memories=None):
        self.enabled = True
        self.memories = list(memories or [])
        self.add_calls = []
        self.update_calls = []
        self.delete_calls = []
        self.restore_calls = []

    @staticmethod
    def _flatten_item_metadata(item):
        payload = dict((item or {}).get("metadata") or {})
        for key in ("user_id", "agent_id", "run_id", "actor_id", "role", "created_at", "updated_at", "hash"):
            value = (item or {}).get(key)
            if value is not None:
                payload[key] = value
        return payload

    async def get_all_memories(self, limit: int = 200):
        return list(self.memories[:limit])

    async def get_memory(self, memory_id: str):
        for item in self.memories:
            if item["id"] == memory_id:
                return item
        return None

    async def add_text(self, text: str, metadata: dict, role: str = "assistant"):
        memory_id = f"new-{len(self.add_calls) + 1}"
        self.add_calls.append((text, dict(metadata), role))
        self.memories.append({
            "id": memory_id,
            "memory": text,
            "metadata": dict(metadata),
            "role": role,
        })
        return {"results": [{"id": memory_id, "memory": text}]}

    async def update_memory(self, memory_id: str, text: str, metadata: dict | None = None):
        self.update_calls.append((memory_id, text, dict(metadata or {})))
        for item in self.memories:
            if item["id"] == memory_id:
                item["memory"] = text
                item["metadata"] = dict(metadata or {})
                break
        return {"message": "ok"}

    async def delete_memory(self, memory_id: str):
        self.delete_calls.append(memory_id)
        self.memories = [item for item in self.memories if item["id"] != memory_id]
        return {"message": "ok"}

    async def restore_memory(self, memory_id: str, text: str, metadata: dict | None = None):
        self.restore_calls.append((memory_id, text, dict(metadata or {})))
        self.memories.append({
            "id": memory_id,
            "memory": text,
            "metadata": dict(metadata or {}),
        })
        return {"message": "ok"}


class TestDreamRun:
    async def test_noop_when_no_unprocessed_history(self, dream, mock_provider, mock_runner, store):
        """Dream should not call LLM when there's nothing to process."""
        result = await dream.run()
        assert result is False
        mock_provider.chat_with_retry.assert_not_called()
        mock_runner.run.assert_not_called()

    async def test_calls_runner_for_unprocessed_entries(self, dream, mock_provider, mock_runner, store):
        """Dream should call AgentRunner when there are unprocessed history entries."""
        store.append_history("User prefers dark mode")
        mock_provider.chat_with_retry.return_value = MagicMock(content="New fact")
        mock_runner.run = AsyncMock(return_value=_make_run_result(
            tool_events=[{"name": "edit_file", "status": "ok", "detail": "memory/MEMORY.md"}],
        ))
        result = await dream.run()
        assert result is True
        mock_runner.run.assert_called_once()
        spec = mock_runner.run.call_args[0][0]
        assert spec.max_iterations == 10
        assert spec.fail_on_tool_error is False

    async def test_advances_dream_cursor(self, dream, mock_provider, mock_runner, store):
        """Dream should advance the cursor after processing."""
        store.append_history("event 1")
        store.append_history("event 2")
        mock_provider.chat_with_retry.return_value = MagicMock(content="Nothing new")
        mock_runner.run = AsyncMock(return_value=_make_run_result())
        await dream.run()
        assert store.get_last_dream_cursor() == 2

    async def test_skips_non_durable_workflow_history_and_still_advances_cursor(
        self,
        dream,
        mock_provider,
        mock_runner,
        store,
    ):
        store.append_history(
            "[Scheduled Task] Timer finished. Task 'bilibili_daily_share' has been triggered. "
            "Use the bilibili-daily-share skill and return exactly NO_SHARE."
        )

        result = await dream.run()

        assert result is True
        assert store.get_last_dream_cursor() == 1
        mock_provider.chat_with_retry.assert_not_called()
        mock_runner.run.assert_not_called()

    async def test_compacts_processed_history(self, dream, mock_provider, mock_runner, store):
        """Dream should compact history after processing."""
        store.append_history("event 1")
        store.append_history("event 2")
        store.append_history("event 3")
        mock_provider.chat_with_retry.return_value = MagicMock(content="Nothing new")
        mock_runner.run = AsyncMock(return_value=_make_run_result())
        await dream.run()
        # After Dream, cursor is advanced and 3, compact keeps last max_history_entries
        entries = store.read_unprocessed_history(since_cursor=0)
        assert all(e["cursor"] > 0 for e in entries)

    async def test_prefers_soul_and_user_when_markdown_memory_is_weak(self, tmp_path, mock_provider, mock_runner):
        """Dream should tell the model that semantic memory is primary when markdown writes are weak."""
        store = MemoryStore(
            tmp_path,
            markdown_config=MarkdownMemoryConfig(persist_long_term=False),
        )
        store.write_soul("# Soul\n- Helpful")
        store.write_user("# User\n- Developer")
        store.write_memory("# Memory\n- Keep this minimal")
        store.append_history("User prefers concise responses and values durable memory recall.")

        dream = Dream(store=store, provider=mock_provider, model="test-model", max_batch_size=5)
        dream._runner = mock_runner

        mock_provider.chat_with_retry.return_value = MagicMock(content="No major changes")
        mock_runner.run = AsyncMock(return_value=_make_run_result())

        result = await dream.run()

        assert result is True
        phase1_prompt = mock_provider.chat_with_retry.await_args.kwargs["messages"][1]["content"]
        assert "Semantic memory is the primary long-term store." in phase1_prompt
        assert "Treat MEMORY.md as an optional, minimal human-readable layer." in phase1_prompt

    async def test_routes_user_profile_to_user_and_cleans_mem0(self, store, mock_provider, mock_runner):
        raw_profile = (
            "- User located in Shanghai, China (UTC+8 timezone)\n"
            "- User opinion: Google authentication is troublesome\n"
            "- User is military aviation enthusiast"
        )
        store.append_history(raw_profile)

        async def _mutate_soul(*args, **kwargs):
            store.write_soul(
                store.read_soul()
                + "\n## 用户画像\n\n**阿钖**\n- 常驻上海\n"
            )
            return _make_run_result(
                tool_events=[{"name": "edit_file", "status": "ok", "detail": "SOUL.md"}],
            )

        mock_provider.chat_with_retry = AsyncMock(side_effect=[
            MagicMock(content="Profile facts detected"),
            MagicMock(content=json.dumps({
                "user_summary": ["常驻上海（UTC+8）", "讨厌 Google 认证折腾", "军事航空爱好者"],
                "mem0_actions": [
                    {
                        "action": "add",
                        "text": "用户常驻上海（UTC+8）。",
                        "category": "user_profile",
                        "origin_ids": ["raw-1"],
                    },
                    {
                        "action": "delete",
                        "id": "raw-1",
                        "reason": "verbose",
                    },
                ],
            }, ensure_ascii=False)),
        ])
        mock_runner.run = AsyncMock(side_effect=_mutate_soul)
        semantic = _FakeSemanticMemory(memories=[{
            "id": "raw-1",
            "memory": raw_profile,
            "created_at": "2026-04-06T11:14:47+00:00",
            "updated_at": "2026-04-06T11:14:47+00:00",
            "metadata": {"source": "history_entry"},
            "role": "assistant",
        }])

        dream = Dream(
            store=store,
            provider=mock_provider,
            model="test-model",
            semantic_memory=semantic,
            max_batch_size=5,
        )
        dream._runner = mock_runner

        result = await dream.run()

        assert result is True
        assert "用户画像" not in store.read_soul()
        assert "常驻上海（UTC+8）" in store.read_user()
        assert "Google 认证折腾" in store.read_user()
        assert semantic.add_calls[0][0] == "用户常驻上海（UTC+8）。"
        assert semantic.add_calls[0][1]["source"] == "dream_profile"
        assert semantic.delete_calls == ["raw-1"]

    async def test_can_run_semantic_housekeeping_without_new_history(self, store, mock_provider, mock_runner):
        mock_provider.chat_with_retry = AsyncMock(return_value=MagicMock(content=json.dumps({
            "user_summary": [],
            "mem0_actions": [
                {"action": "delete", "id": "auto-1", "reason": "duplicate"},
            ],
        }, ensure_ascii=False)))
        mock_runner.run = AsyncMock()
        semantic = _FakeSemanticMemory(memories=[{
            "id": "auto-1",
            "memory": "用户把 nanobot 当作生活助手，不只限工作。",
            "created_at": "2026-04-06T10:00:00+00:00",
            "updated_at": "2026-04-06T10:00:00+00:00",
            "metadata": {"source": "auto_turn", "category": "assistant_role"},
            "role": "assistant",
        }])
        dream = Dream(
            store=store,
            provider=mock_provider,
            model="test-model",
            semantic_memory=semantic,
            max_batch_size=5,
        )
        dream._runner = mock_runner

        result = await dream.run()

        assert result is True
        mock_runner.run.assert_not_called()
        assert semantic.delete_calls == ["auto-1"]

    async def test_restore_batch_reverts_markdown_and_mem0(self, store, mock_provider, mock_runner):
        store.git.init()
        original_user = store.read_user()
        store.write_user("# 用户画像\n\n## 稳定画像\n- 常驻上海（UTC+8）\n")
        commit_sha = store.git.auto_commit("dream: batch")
        store.append_dream_batch({
            "batch_id": "20260406220000-abcd1234",
            "kind": "dream",
            "created_at": "2026-04-06T22:00:00+08:00",
            "git_commit": commit_sha,
            "changed_files": ["USER.md"],
            "mem0_actions": [
                {
                    "op": "add",
                    "memory_id": "mem-1",
                    "old_text": None,
                    "new_text": "用户常驻上海（UTC+8）。",
                    "old_metadata": None,
                    "new_metadata": {"source": "dream_profile", "managed_by": "dream"},
                },
            ],
        })
        semantic = _FakeSemanticMemory(memories=[{
            "id": "mem-1",
            "memory": "用户常驻上海（UTC+8）。",
            "metadata": {"source": "dream_profile", "managed_by": "dream"},
        }])
        dream = Dream(
            store=store,
            provider=mock_provider,
            model="test-model",
            semantic_memory=semantic,
        )

        restored = await dream.restore_batch(commit_sha)

        assert restored is not None
        assert semantic.delete_calls == ["mem-1"]
        assert restored["new_git_sha"] is not None
        assert store.read_user().strip() == original_user.strip()
        assert store.read_dream_batches()[-1]["kind"] == "restore"

