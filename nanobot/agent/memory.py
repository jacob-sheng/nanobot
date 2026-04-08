"""Memory system: pure file I/O store, lightweight Consolidator, and Dream processor."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
import weakref
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanobot.config.schema import MarkdownMemoryConfig
from nanobot.utils.prompt_templates import render_template
from nanobot.utils.helpers import ensure_dir, estimate_message_tokens, estimate_prompt_tokens_chain, strip_think

from nanobot.agent.runner import AgentRunSpec, AgentRunner
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.utils.gitstore import GitStore

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider
    from nanobot.agent.semantic_memory import SemanticMemory
    from nanobot.session.manager import Session, SessionManager


_SAVE_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save the memory consolidation result to persistent storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "history_entry": {
                        "type": "string",
                        "description": "A paragraph summarizing key events/decisions/topics. "
                        "Start with [YYYY-MM-DD HH:MM]. Include detail useful for search.",
                    },
                    "memory_update": {
                        "type": "string",
                        "description": "Full updated long-term memory as markdown. Include all "
                        "existing facts plus new ones. Return unchanged if nothing new.",
                    },
                },
                "required": ["history_entry", "memory_update"],
            },
        },
    }
]

_TOOL_CHOICE_ERROR_MARKERS = (
    "tool_choice",
    "toolchoice",
    "does not support",
    'should be ["none", "auto"]',
)


def _ensure_text(value: Any) -> str:
    """Normalize tool-call payload values to text for file storage."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _normalize_save_memory_args(args: Any) -> dict[str, Any] | None:
    """Normalize provider tool-call arguments to the expected dict shape."""
    if isinstance(args, str):
        args = json.loads(args)
    if isinstance(args, list):
        return args[0] if args and isinstance(args[0], dict) else None
    return args if isinstance(args, dict) else None


def _is_tool_choice_unsupported(content: str | None) -> bool:
    """Detect provider errors caused by forced tool_choice being unsupported."""
    text = (content or "").lower()
    return any(marker in text for marker in _TOOL_CHOICE_ERROR_MARKERS)


_NON_DURABLE_WORKFLOW_MARKERS = (
    "[scrubbed_non_durable_workflow]",
    "use the mastodon-daily-share skill",
    "use the bilibili-daily-share skill",
    "mastodon home timeline",
    "bilibili home recommendations",
    "record the shared status ids",
    "record the shared bvids",
    "mark-shared",
    "nothing worth sending today",
)
_NON_DURABLE_WORKFLOW_PATTERNS = (
    re.compile(r"\b(?:mastodon|bilibili)_daily_share\b", re.IGNORECASE),
    re.compile(r"\bno_share\b", re.IGNORECASE),
    re.compile(r"\blogin_required\s*:", re.IGNORECASE),
)


def is_non_durable_workflow_content(text: Any) -> bool:
    """Return whether text is a workflow artifact that should not enter durable memory."""
    content = " ".join(str(text or "").lower().split())
    if not content:
        return False
    if any(marker in content for marker in _NON_DURABLE_WORKFLOW_MARKERS):
        return True
    return any(pattern.search(content) for pattern in _NON_DURABLE_WORKFLOW_PATTERNS)


# ---------------------------------------------------------------------------
# MemoryStore — pure file I/O layer
# ---------------------------------------------------------------------------

class MemoryStore:
    """Pure file I/O for memory files: MEMORY.md, history.jsonl, SOUL.md, USER.md."""

    _DEFAULT_MAX_HISTORY = 1000
    _MAX_FAILURES_BEFORE_RAW_ARCHIVE = 3
    _LEGACY_ENTRY_START_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}[^\]]*)\]\s*")
    _LEGACY_TIMESTAMP_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]\s*")
    _LEGACY_RAW_MESSAGE_RE = re.compile(
        r"^\[\d{4}-\d{2}-\d{2}[^\]]*\]\s+[A-Z][A-Z0-9_]*(?:\s+\[tools:\s*[^\]]+\])?:"
    )

    def __init__(
        self,
        workspace: Path,
        max_history_entries: int = _DEFAULT_MAX_HISTORY,
        markdown_config: MarkdownMemoryConfig | None = None,
    ):
        self.workspace = workspace
        self.max_history_entries = max_history_entries
        self.markdown_config = markdown_config or MarkdownMemoryConfig()
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "history.jsonl"
        self.legacy_history_file = self.memory_dir / "HISTORY.md"
        self.soul_file = workspace / "SOUL.md"
        self.user_file = workspace / "USER.md"
        self._cursor_file = self.memory_dir / ".cursor"
        self._dream_cursor_file = self.memory_dir / ".dream_cursor"
        self._dream_batches_file = self.memory_dir / "dream_batches.jsonl"
        self._dream_semantic_state_file = self.memory_dir / ".dream_semantic_state.json"
        self._consecutive_failures = 0
        self._git = GitStore(workspace, tracked_files=[
            "SOUL.md", "USER.md", "memory/MEMORY.md",
        ])
        self._maybe_migrate_legacy_history()

    @property
    def git(self) -> GitStore:
        return self._git

    # -- generic helpers -----------------------------------------------------

    @staticmethod
    def read_file(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def _maybe_migrate_legacy_history(self) -> None:
        """One-time upgrade from legacy HISTORY.md to history.jsonl.

        The migration is best-effort and prioritizes preserving as much content
        as possible over perfect parsing.
        """
        legacy_exists = self.legacy_history_file.exists()
        if legacy_exists is not True:
            return
        history_exists = self.history_file.exists()
        if history_exists is True:
            try:
                existing_size = self.history_file.stat().st_size
            except OSError:
                existing_size = 0
            if isinstance(existing_size, int | float) and existing_size > 0:
                return

        try:
            legacy_text = self.legacy_history_file.read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            logger.exception("Failed to read legacy HISTORY.md for migration")
            return

        entries = self._parse_legacy_history(legacy_text)
        try:
            if entries:
                self._write_entries(entries)
                last_cursor = entries[-1]["cursor"]
                self._cursor_file.write_text(str(last_cursor), encoding="utf-8")
                # Default to "already processed" so upgrades do not replay the
                # user's entire historical archive into Dream on first start.
                self._dream_cursor_file.write_text(str(last_cursor), encoding="utf-8")

            backup_path = self._next_legacy_backup_path()
            self.legacy_history_file.replace(backup_path)
            logger.info(
                "Migrated legacy HISTORY.md to history.jsonl ({} entries)",
                len(entries),
            )
        except Exception:
            logger.exception("Failed to migrate legacy HISTORY.md")

    def _parse_legacy_history(self, text: str) -> list[dict[str, Any]]:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            return []

        fallback_timestamp = self._legacy_fallback_timestamp()
        entries: list[dict[str, Any]] = []
        chunks = self._split_legacy_history_chunks(normalized)

        for cursor, chunk in enumerate(chunks, start=1):
            timestamp = fallback_timestamp
            content = chunk
            match = self._LEGACY_TIMESTAMP_RE.match(chunk)
            if match:
                timestamp = match.group(1)
                remainder = chunk[match.end():].lstrip()
                if remainder:
                    content = remainder

            entries.append({
                "cursor": cursor,
                "timestamp": timestamp,
                "content": content,
            })
        return entries

    def _split_legacy_history_chunks(self, text: str) -> list[str]:
        lines = text.split("\n")
        chunks: list[str] = []
        current: list[str] = []
        saw_blank_separator = False

        for line in lines:
            if saw_blank_separator and line.strip() and current:
                chunks.append("\n".join(current).strip())
                current = [line]
                saw_blank_separator = False
                continue
            if self._should_start_new_legacy_chunk(line, current):
                chunks.append("\n".join(current).strip())
                current = [line]
                saw_blank_separator = False
                continue
            current.append(line)
            saw_blank_separator = not line.strip()

        if current:
            chunks.append("\n".join(current).strip())
        return [chunk for chunk in chunks if chunk]

    def _should_start_new_legacy_chunk(self, line: str, current: list[str]) -> bool:
        if not current:
            return False
        if not self._LEGACY_ENTRY_START_RE.match(line):
            return False
        if self._is_raw_legacy_chunk(current) and self._LEGACY_RAW_MESSAGE_RE.match(line):
            return False
        return True

    def _is_raw_legacy_chunk(self, lines: list[str]) -> bool:
        first_nonempty = next((line for line in lines if line.strip()), "")
        match = self._LEGACY_TIMESTAMP_RE.match(first_nonempty)
        if not match:
            return False
        return first_nonempty[match.end():].lstrip().startswith("[RAW]")

    def _legacy_fallback_timestamp(self) -> str:
        try:
            return datetime.fromtimestamp(
                self.legacy_history_file.stat().st_mtime,
            ).strftime("%Y-%m-%d %H:%M")
        except OSError:
            return datetime.now().strftime("%Y-%m-%d %H:%M")

    def _next_legacy_backup_path(self) -> Path:
        candidate = self.memory_dir / "HISTORY.md.bak"
        suffix = 2
        while candidate.exists():
            candidate = self.memory_dir / f"HISTORY.md.bak.{suffix}"
            suffix += 1
        return candidate

    # -- MEMORY.md (long-term facts) -----------------------------------------

    def read_memory(self) -> str:
        if not self.markdown_config.enabled:
            return ""
        return self.read_file(self.memory_file)

    def read_long_term(self) -> str:
        """Backward-compatible alias for the markdown long-term memory file."""
        return self.read_memory()

    def write_memory(self, content: str) -> None:
        if not (self.markdown_config.enabled and self.markdown_config.persist_long_term):
            return
        self.memory_file.write_text(content, encoding="utf-8")

    def write_long_term(self, content: str) -> None:
        """Backward-compatible alias for the markdown long-term memory file."""
        self.write_memory(content)

    # -- SOUL.md -------------------------------------------------------------

    def read_soul(self) -> str:
        return self.read_file(self.soul_file)

    def write_soul(self, content: str) -> None:
        self.soul_file.write_text(content, encoding="utf-8")

    # -- USER.md -------------------------------------------------------------

    def read_user(self) -> str:
        return self.read_file(self.user_file)

    def write_user(self, content: str) -> None:
        self.user_file.write_text(content, encoding="utf-8")

    # -- context injection (used by context.py) ------------------------------

    def get_memory_context(self) -> str:
        if not (
            self.markdown_config.enabled
            and self.markdown_config.load_long_term_into_context
        ):
            return ""
        long_term = self.read_memory()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    # -- history.jsonl — append-only, JSONL format ---------------------------

    def append_history(self, entry: str | dict[str, Any]) -> int:
        """Append *entry* to history.jsonl and return its auto-incrementing cursor."""
        if not (self.markdown_config.enabled and self.markdown_config.persist_history):
            return 0
        cursor = self._next_cursor()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        if isinstance(entry, dict):
            record = dict(entry)
            record.setdefault("cursor", cursor)
            record.setdefault("timestamp", record.get("timestamp") or ts)
            if "content" not in record:
                summary = record.get("summary")
                record["content"] = (
                    summary if isinstance(summary, str) else json.dumps(entry, ensure_ascii=False)
                )
        else:
            record = {
                "cursor": cursor,
                "timestamp": ts,
                "content": strip_think(entry.rstrip()) or entry.rstrip(),
            }
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._cursor_file.write_text(str(cursor), encoding="utf-8")
        return cursor

    def _next_cursor(self) -> int:
        """Read the current cursor counter and return next value."""
        if self._cursor_file.exists():
            try:
                return int(self._cursor_file.read_text(encoding="utf-8").strip()) + 1
            except (ValueError, OSError):
                pass
        # Fallback: read last line's cursor from the JSONL file.
        last = self._read_last_entry()
        if last:
            return last["cursor"] + 1
        return 1

    def read_unprocessed_history(self, since_cursor: int) -> list[dict[str, Any]]:
        """Return history entries with cursor > *since_cursor*."""
        return [e for e in self._read_entries() if e["cursor"] > since_cursor]

    def compact_history(self) -> None:
        """Drop oldest entries if the file exceeds *max_history_entries*."""
        if self.max_history_entries <= 0:
            return
        entries = self._read_entries()
        if len(entries) <= self.max_history_entries:
            return
        kept = entries[-self.max_history_entries:]
        self._write_entries(kept)

    # -- JSONL helpers -------------------------------------------------------

    def _read_entries(self) -> list[dict[str, Any]]:
        """Read all entries from history.jsonl."""
        entries: list[dict[str, Any]] = []
        try:
            with open(self.history_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except FileNotFoundError:
            pass
        return entries

    def _read_last_entry(self) -> dict[str, Any] | None:
        """Read the last entry from the JSONL file efficiently."""
        try:
            with open(self.history_file, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if size == 0:
                    return None
                read_size = min(size, 4096)
                f.seek(size - read_size)
                data = f.read().decode("utf-8")
                lines = [l for l in data.split("\n") if l.strip()]
                if not lines:
                    return None
                return json.loads(lines[-1])
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def _write_entries(self, entries: list[dict[str, Any]]) -> None:
        """Overwrite history.jsonl with the given entries."""
        with open(self.history_file, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # -- dream cursor --------------------------------------------------------

    def get_last_dream_cursor(self) -> int:
        if self._dream_cursor_file.exists():
            try:
                return int(self._dream_cursor_file.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                pass
        return 0

    def set_last_dream_cursor(self, cursor: int) -> None:
        self._dream_cursor_file.write_text(str(cursor), encoding="utf-8")

    # -- dream batch audit --------------------------------------------------

    def append_dream_batch(self, record: dict[str, Any]) -> None:
        with open(self._dream_batches_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def read_dream_batches(self) -> list[dict[str, Any]]:
        batches: list[dict[str, Any]] = []
        try:
            with open(self._dream_batches_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        batches.append(payload)
        except FileNotFoundError:
            pass
        return batches

    def find_dream_batch(self, identifier: str | None = None) -> dict[str, Any] | None:
        batches = self.read_dream_batches()
        if not batches:
            return None
        if not identifier:
            return batches[-1]
        ident = identifier.strip().lower()
        for batch in reversed(batches):
            batch_id = str(batch.get("batch_id") or "").lower()
            commit = str(batch.get("git_commit") or "").lower()
            if batch_id.startswith(ident) or (commit and commit.startswith(ident)):
                return batch
        return None

    def get_dream_semantic_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self._dream_semantic_state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"housekeeping_cursor": 0}
        except (json.JSONDecodeError, OSError):
            return {"housekeeping_cursor": 0}
        if not isinstance(payload, dict):
            return {"housekeeping_cursor": 0}
        payload.setdefault("housekeeping_cursor", 0)
        return payload

    def set_dream_semantic_state(self, state: dict[str, Any]) -> None:
        self._dream_semantic_state_file.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    # -- message formatting utility ------------------------------------------

    @staticmethod
    def _format_messages(messages: list[dict]) -> str:
        lines = []
        for message in messages:
            if not message.get("content"):
                continue
            tools = f" [tools: {', '.join(message['tools_used'])}]" if message.get("tools_used") else ""
            lines.append(
                f"[{message.get('timestamp', '?')[:16]}] {message['role'].upper()}{tools}: {message['content']}"
            )
        return "\n".join(lines)

    def raw_archive(self, messages: list[dict]) -> None:
        """Fallback: dump raw messages to history.jsonl without LLM summarization."""
        self.append_history(
            f"[RAW] {len(messages)} messages\n"
            f"{self._format_messages(messages)}"
        )
        logger.warning(
            "Memory consolidation degraded: raw-archived {} messages", len(messages)
        )

    async def consolidate(
        self,
        messages: list[dict],
        provider: LLMProvider,
        model: str,
        on_history_entry: Callable[[str], Awaitable[None]] | None = None,
    ) -> bool:
        """Backward-compatible one-shot consolidation entrypoint."""
        if not messages:
            return True

        current_memory = self.read_memory() if self.markdown_config.persist_long_term else ""
        prompt = f"""Process this conversation and call the save_memory tool with your consolidation.

## Current Long-term Memory
{current_memory or "(empty)"}

## Conversation to Process
{self._format_messages(messages)}"""

        chat_messages = [
            {
                "role": "system",
                "content": (
                    "You are a memory consolidation agent. "
                    "Call the save_memory tool with your consolidation of the conversation."
                ),
            },
            {"role": "user", "content": prompt},
        ]

        try:
            forced = {"type": "function", "function": {"name": "save_memory"}}
            response = await provider.chat_with_retry(
                messages=chat_messages,
                tools=_SAVE_MEMORY_TOOL,
                model=model,
                tool_choice=forced,
            )

            if response.finish_reason == "error" and _is_tool_choice_unsupported(response.content):
                logger.warning("Forced tool_choice unsupported, retrying with auto")
                response = await provider.chat_with_retry(
                    messages=chat_messages,
                    tools=_SAVE_MEMORY_TOOL,
                    model=model,
                    tool_choice="auto",
                )

            if not response.has_tool_calls:
                logger.warning(
                    "Memory consolidation: LLM did not call save_memory "
                    "(finish_reason={}, content_len={}, content_preview={})",
                    response.finish_reason,
                    len(response.content or ""),
                    (response.content or "")[:200],
                )
                return await self._fail_or_raw_archive(messages, on_history_entry=on_history_entry)

            args = _normalize_save_memory_args(response.tool_calls[0].arguments)
            if args is None:
                logger.warning("Memory consolidation: unexpected save_memory arguments")
                return await self._fail_or_raw_archive(messages, on_history_entry=on_history_entry)

            if "history_entry" not in args or "memory_update" not in args:
                logger.warning("Memory consolidation: save_memory payload missing required fields")
                return await self._fail_or_raw_archive(messages, on_history_entry=on_history_entry)

            entry_value = args["history_entry"]
            update = args["memory_update"]
            if entry_value is None or update is None:
                logger.warning("Memory consolidation: save_memory payload contains null required fields")
                return await self._fail_or_raw_archive(messages, on_history_entry=on_history_entry)

            entry = _ensure_text(entry_value).strip()
            if not entry:
                logger.warning("Memory consolidation: history_entry is empty after normalization")
                return await self._fail_or_raw_archive(messages, on_history_entry=on_history_entry)

            self.append_history(entry_value if isinstance(entry_value, dict) else entry)
            if on_history_entry:
                try:
                    await on_history_entry(entry)
                except Exception:
                    logger.exception("Semantic history sync failed after consolidation")

            update = _ensure_text(update)
            if self.markdown_config.persist_long_term and update != current_memory:
                self.write_memory(update)

            self._consecutive_failures = 0
            logger.info("Memory consolidation done for {} messages", len(messages))
            return True
        except Exception:
            logger.exception("Memory consolidation failed")
            return await self._fail_or_raw_archive(messages, on_history_entry=on_history_entry)

    async def _fail_or_raw_archive(
        self,
        messages: list[dict],
        on_history_entry: Callable[[str], Awaitable[None]] | None = None,
    ) -> bool:
        """Increment failure count; after threshold, raw-archive messages and return True."""
        self._consecutive_failures += 1
        if self._consecutive_failures < self._MAX_FAILURES_BEFORE_RAW_ARCHIVE:
            return False

        entry = self._build_raw_archive_entry(messages)
        self.append_history(entry)
        if on_history_entry:
            try:
                await on_history_entry(entry)
            except Exception:
                logger.exception("Semantic raw-archive sync failed")
        self._consecutive_failures = 0
        return True

    def _build_raw_archive_entry(self, messages: list[dict]) -> str:
        """Build a raw-archive entry without requiring Markdown persistence."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"[{ts}] [RAW] {len(messages)} messages\n{self._format_messages(messages)}"
        logger.warning(
            "Memory consolidation degraded: raw-archived {} messages", len(messages)
        )
        return entry



# ---------------------------------------------------------------------------
# Consolidator — lightweight token-budget triggered consolidation
# ---------------------------------------------------------------------------


class Consolidator:
    """Lightweight consolidation: summarizes evicted messages into history.jsonl."""

    _MAX_CONSOLIDATION_ROUNDS = 5

    _SAFETY_BUFFER = 1024  # extra headroom for tokenizer estimation drift

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMProvider,
        model: str,
        sessions: SessionManager,
        context_window_tokens: int,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
        max_completion_tokens: int = 4096,
        on_history_entry: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.sessions = sessions
        self.context_window_tokens = context_window_tokens
        self.max_completion_tokens = max_completion_tokens
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self._on_history_entry = on_history_entry
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    def get_lock(self, session_key: str) -> asyncio.Lock:
        """Return the shared consolidation lock for one session."""
        return self._locks.setdefault(session_key, asyncio.Lock())

    def pick_consolidation_boundary(
        self,
        session: Session,
        tokens_to_remove: int,
    ) -> tuple[int, int] | None:
        """Pick a user-turn boundary that removes enough old prompt tokens."""
        start = session.last_consolidated
        if start >= len(session.messages) or tokens_to_remove <= 0:
            return None

        removed_tokens = 0
        last_boundary: tuple[int, int] | None = None
        for idx in range(start, len(session.messages)):
            message = session.messages[idx]
            if idx > start and message.get("role") == "user":
                last_boundary = (idx, removed_tokens)
                if removed_tokens >= tokens_to_remove:
                    return last_boundary
            removed_tokens += estimate_message_tokens(message)

        return last_boundary

    def estimate_session_prompt_tokens(self, session: Session) -> tuple[int, str]:
        """Estimate current prompt size for the normal session history view."""
        history = session.get_history(max_messages=0)
        channel, chat_id = (session.key.split(":", 1) if ":" in session.key else (None, None))
        probe_messages = self._build_messages(
            history=history,
            current_message="[token-probe]",
            channel=channel,
            chat_id=chat_id,
        )
        return estimate_prompt_tokens_chain(
            self.provider,
            self.model,
            probe_messages,
            self._get_tool_definitions(),
        )

    @staticmethod
    def _session_memory_policy(session: Session) -> str:
        return str((session.metadata or {}).get("memory_policy") or "durable").lower()

    def _skip_durable_archive_for_session(self, session: Session) -> bool:
        if self._session_memory_policy(session) != "transient":
            return False

        unconsolidated = len(session.messages) - session.last_consolidated
        if unconsolidated <= 0:
            return True

        session.last_consolidated = len(session.messages)
        self.sessions.save(session)
        logger.info(
            "Token consolidation skipped durable archive for {} due to transient memory policy ({} msgs)",
            session.key,
            unconsolidated,
        )
        return True

    async def archive(self, messages: list[dict]) -> bool:
        """Summarize messages via LLM and append to history.jsonl.

        Returns True on success (or degraded success), False if nothing to do.
        """
        if not messages:
            return False
        try:
            formatted = MemoryStore._format_messages(messages)
            response = await self.provider.chat_with_retry(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": render_template(
                            "agent/consolidator_archive.md",
                            strip=True,
                        ),
                    },
                    {"role": "user", "content": formatted},
                ],
                tools=None,
                tool_choice=None,
            )
            summary = response.content or "[no summary]"
            self.store.append_history(summary)
            if self._on_history_entry:
                try:
                    await self._on_history_entry(summary)
                except Exception:
                    logger.exception("Semantic history sync failed after consolidation")
            return True
        except Exception:
            logger.warning("Consolidation LLM call failed, raw-dumping to history")
            self.store.raw_archive(messages)
            return True

    async def maybe_consolidate_by_tokens(self, session: Session) -> None:
        """Loop: archive old messages until prompt fits within safe budget.

        The budget reserves space for completion tokens and a safety buffer
        so the LLM request never exceeds the context window.
        """
        if not session.messages or not self.context_window_tokens or self.context_window_tokens <= 0:
            return

        lock = self.get_lock(session.key)
        async with lock:
            if self._skip_durable_archive_for_session(session):
                return

            budget = self.context_window_tokens - self.max_completion_tokens - self._SAFETY_BUFFER
            target = budget // 2
            estimated, source = self.estimate_session_prompt_tokens(session)
            if estimated <= 0:
                return
            if estimated < budget:
                logger.debug(
                    "Token consolidation idle {}: {}/{} via {}",
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                )
                return

            for round_num in range(self._MAX_CONSOLIDATION_ROUNDS):
                if estimated <= target:
                    return

                boundary = self.pick_consolidation_boundary(session, max(1, estimated - target))
                if boundary is None:
                    logger.debug(
                        "Token consolidation: no safe boundary for {} (round {})",
                        session.key,
                        round_num,
                    )
                    return

                end_idx = boundary[0]
                chunk = session.messages[session.last_consolidated:end_idx]
                if not chunk:
                    return

                logger.info(
                    "Token consolidation round {} for {}: {}/{} via {}, chunk={} msgs",
                    round_num,
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                    len(chunk),
                )
                if not await self.archive(chunk):
                    return
                session.last_consolidated = end_idx
                self.sessions.save(session)

                estimated, source = self.estimate_session_prompt_tokens(session)
                if estimated <= 0:
                    return


# ---------------------------------------------------------------------------
# Dream — heavyweight cron-scheduled memory consolidation
# ---------------------------------------------------------------------------


class Dream:
    """Two-phase memory processor: analyze history.jsonl, then edit files via AgentRunner.

    Phase 1 produces an analysis summary (plain LLM call).
    Phase 2 delegates to AgentRunner with read_file / edit_file tools so the
    LLM can make targeted, incremental edits instead of replacing entire files.
    """

    _USER_PLACEHOLDER = "<!-- User preferences are now defined in SOUL.md -->"
    _MANAGEABLE_SOURCES = {"history_entry", "auto_turn", "dream_profile", "dream_cleanup"}
    _FORBIDDEN_SOUL_HEADINGS = (
        "用户画像",
        "用户偏好",
        "用户档案",
        "stable user profile",
        "user profile",
    )

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMProvider,
        model: str,
        semantic_memory: SemanticMemory | None = None,
        max_batch_size: int = 20,
        max_iterations: int = 10,
        max_tool_result_chars: int = 16_000,
        semantic_housekeeping_limit: int = 8,
        semantic_scan_limit: int = 200,
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.semantic_memory = semantic_memory
        self.max_batch_size = max_batch_size
        self.max_iterations = max_iterations
        self.max_tool_result_chars = max_tool_result_chars
        self.semantic_housekeeping_limit = semantic_housekeeping_limit
        self.semantic_scan_limit = semantic_scan_limit
        self._runner = AgentRunner(provider)
        self._tools = self._build_tools()

    def _build_file_context(self) -> str:
        """Build Dream's editable file context with local memory-policy hints."""
        current_soul = self.store.read_soul() or "(empty)"
        current_user = self.store.read_user() or "(empty)"
        current_memory = self.store.read_memory() or "(empty)"

        sections = []
        sections.append(
            "## Memory Layer Policy\n"
            "- `SOUL.md` is only for Hera's identity, tone, style, and behavioral rules.\n"
            "- Never write user facts, preferences, location, hobbies, or biography into `SOUL.md`.\n"
            "- `USER.md` is the Markdown layer for concise, stable user profile facts.\n"
            "- Keep `USER.md` short and summary-like; detailed evidence belongs in semantic memory instead."
        )
        if not self.store.markdown_config.persist_long_term:
            sections.append(
                "## Memory Policy\n"
                "Semantic memory is the primary long-term store. "
                "Prefer updating SOUL.md and USER.md. "
                "Treat MEMORY.md as an optional, minimal human-readable layer."
            )

        sections.append(f"## Current SOUL.md\n{current_soul}")
        sections.append(f"## Current USER.md\n{current_user}")
        sections.append(f"## Current MEMORY.md\n{current_memory}")
        return "\n\n".join(sections)

    # -- tool registry -------------------------------------------------------

    def _build_tools(self) -> ToolRegistry:
        """Build a minimal tool registry for the Dream agent."""
        from nanobot.agent.tools.filesystem import EditFileTool, ReadFileTool

        tools = ToolRegistry()
        workspace = self.store.workspace
        tools.register(ReadFileTool(workspace=workspace, allowed_dir=workspace))
        tools.register(EditFileTool(workspace=workspace, allowed_dir=workspace))
        return tools

    @staticmethod
    def _new_batch_id() -> str:
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        return f"{ts}-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _normalize_text(text: str) -> str:
        return " ".join(str(text or "").split()).strip().lower()

    @classmethod
    def _is_placeholder_user_file(cls, content: str) -> bool:
        return cls._normalize_text(content) == cls._normalize_text(cls._USER_PLACEHOLDER)

    @classmethod
    def _clean_user_summary_lines(cls, lines: list[str]) -> list[str]:
        seen: set[str] = set()
        cleaned: list[str] = []
        for line in lines:
            text = re.sub(r"^\s*[-*]\s*", "", str(line or "")).strip()
            if not text:
                continue
            norm = cls._normalize_text(text)
            if norm in seen:
                continue
            seen.add(norm)
            cleaned.append(text)
            if len(cleaned) >= 6:
                break
        return cleaned

    @classmethod
    def _format_user_markdown(cls, lines: list[str]) -> str:
        cleaned = cls._clean_user_summary_lines(lines)
        if not cleaned:
            return cls._USER_PLACEHOLDER + "\n"
        bullets = "\n".join(f"- {line}" for line in cleaned)
        return "# 用户画像\n\n## 稳定画像\n" + bullets + "\n"

    def _write_user_summary(self, lines: list[str]) -> bool:
        desired = self._format_user_markdown(lines)
        current = self.store.read_user()
        if current.strip() == desired.strip():
            return False
        self.store.write_user(desired)
        return True

    @classmethod
    def _strip_forbidden_soul_sections(cls, content: str) -> tuple[str, bool]:
        lines = content.splitlines()
        output: list[str] = []
        skip = False
        skip_level = 0
        changed = False
        for line in lines:
            heading = re.match(r"^(#{1,6})\s*(.+?)\s*$", line)
            if heading:
                level = len(heading.group(1))
                title = heading.group(2).strip().lower()
                if skip and level <= skip_level:
                    skip = False
                    skip_level = 0
                if any(marker in title for marker in cls._FORBIDDEN_SOUL_HEADINGS):
                    skip = True
                    skip_level = level
                    changed = True
                    continue
            if skip:
                changed = True
                continue
            output.append(line)
        normalized = re.sub(r"\n{3,}", "\n\n", "\n".join(output)).strip()
        if normalized:
            normalized += "\n"
        return normalized, changed

    @staticmethod
    def _extract_json_object(raw: str) -> dict[str, Any] | None:
        text = str(raw or "").strip()
        if not text:
            return None
        cleaned = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        candidates = [cleaned]
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            candidates.insert(0, match.group(0))
        for candidate in candidates:
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
        return None

    @staticmethod
    def _item_metadata(item: dict[str, Any] | None) -> dict[str, Any]:
        return dict((item or {}).get("metadata") or {})

    @classmethod
    def _item_source(cls, item: dict[str, Any] | None) -> str:
        return str(cls._item_metadata(item).get("source") or "").lower()

    @classmethod
    def _is_manageable_memory(cls, item: dict[str, Any]) -> bool:
        metadata = cls._item_metadata(item)
        source = str(metadata.get("source") or "").lower()
        managed_by = str(metadata.get("managed_by") or "").lower()
        return managed_by == "dream" or source in cls._MANAGEABLE_SOURCES

    @staticmethod
    def _sort_memory_key(item: dict[str, Any]) -> tuple[str, str]:
        updated = str(item.get("updated_at") or item.get("created_at") or "")
        return updated, str(item.get("id") or "")

    async def _collect_semantic_candidates(self, batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self.semantic_memory or not self.semantic_memory.enabled:
            return []

        memories = await self.semantic_memory.get_all_memories(limit=self.semantic_scan_limit)
        manageable = [item for item in memories if self._is_manageable_memory(item)]
        if not manageable:
            return []

        matched: list[dict[str, Any]] = []
        batch_texts = {str(entry.get("content") or "") for entry in batch}
        for item in manageable:
            if is_non_durable_workflow_content(item.get("memory")):
                continue
            if self._item_source(item) == "history_entry" and str(item.get("memory") or "") in batch_texts:
                matched.append(item)
        matched_ids = {str(item.get("id")) for item in matched}

        remaining = sorted(
            [item for item in manageable if str(item.get("id")) not in matched_ids],
            key=self._sort_memory_key,
        )
        state = self.store.get_dream_semantic_state()
        start = int(state.get("housekeeping_cursor", 0) or 0)
        housekeeping: list[dict[str, Any]] = []
        if remaining:
            start = start % len(remaining)
            housekeeping = remaining[start:start + self.semantic_housekeeping_limit]
            if len(housekeeping) < self.semantic_housekeeping_limit and start:
                housekeeping.extend(remaining[: self.semantic_housekeeping_limit - len(housekeeping)])
            state["housekeeping_cursor"] = (start + len(housekeeping)) % len(remaining)
        else:
            state["housekeeping_cursor"] = 0
        state["updated_at"] = datetime.now().isoformat()
        self.store.set_dream_semantic_state(state)

        combined: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in matched + housekeeping:
            memory_id = str(item.get("id") or "")
            if not memory_id or memory_id in seen:
                continue
            seen.add(memory_id)
            combined.append(item)
        return combined

    def _build_semantic_cleanup_prompt(
        self,
        analysis: str,
        batch: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        candidate_payload = []
        for item in candidates:
            metadata = self._item_metadata(item)
            candidate_payload.append({
                "id": item.get("id"),
                "memory": item.get("memory"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "source": metadata.get("source"),
                "category": metadata.get("category"),
                "managed_by": metadata.get("managed_by"),
                "origin_memory_ids": metadata.get("origin_memory_ids"),
            })
        history_text = "\n".join(
            f"[{entry['timestamp']}] {entry['content']}" for entry in batch
        ) or "(no new history entries)"
        system = (
            "You are Dream's semantic memory curator.\n"
            "Return strict JSON with keys `user_summary` and `mem0_actions`.\n"
            "`user_summary` must be an array of at most 6 short, stable user-profile bullets.\n"
            "`mem0_actions` must be an array of objects using one of:\n"
            "- {\"action\":\"add\",\"text\":\"...\",\"category\":\"user_profile|preference|identity|project_context\",\"origin_ids\":[...],\"reason\":\"...\"}\n"
            "- {\"action\":\"update\",\"id\":\"...\",\"text\":\"...\",\"category\":\"...\",\"origin_ids\":[...],\"reason\":\"...\"}\n"
            "- {\"action\":\"delete\",\"id\":\"...\",\"reason\":\"duplicate|verbose|outdated|wrong_layer\"}\n"
            "Rules:\n"
            "- SOUL.md is never for user facts.\n"
            "- USER.md is a concise index only; no biography, no quoted chat lines, no evidence chains.\n"
            "- Canonical semantic memories must be short, durable, and one fact per line.\n"
            "- Only update or delete listed candidate ids.\n"
            "- Prefer replacing verbose `history_entry` dumps with shorter canonical facts.\n"
            "- Never modify explicit user-owned memories; assume only listed candidates are writable.\n"
            "- If nothing should change, return empty arrays.\n"
            "Return JSON only."
        )
        user = (
            f"## Dream analysis\n{analysis or '(no analysis)'}\n\n"
            f"## Current USER.md\n{self.store.read_user() or '(empty)'}\n\n"
            f"## Current SOUL.md\n{self.store.read_soul() or '(empty)'}\n\n"
            f"## New history batch\n{history_text}\n\n"
            "## Writable semantic memories\n"
            f"{json.dumps(candidate_payload, ensure_ascii=False, indent=2)}"
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _build_dream_metadata(
        self,
        *,
        current: dict[str, Any] | None,
        batch_id: str,
        category: str | None,
        origin_ids: list[str] | None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        if self.semantic_memory:
            metadata.update(self.semantic_memory._flatten_item_metadata(current))
        current_meta = self._item_metadata(current)
        current_source = str(current_meta.get("source") or "").lower()
        if current_source and current_source not in {"dream_profile", "dream_cleanup"}:
            metadata.setdefault("origin_source", current_source)
        combined_origin_ids = [
            str(item_id)
            for item_id in (
                list(current_meta.get("origin_memory_ids") or [])
                + list(origin_ids or [])
            )
            if str(item_id).strip()
        ]
        if current and current_source not in {"dream_profile", "dream_cleanup"}:
            combined_origin_ids.append(str(current.get("id") or ""))
        deduped_origin_ids: list[str] = []
        seen: set[str] = set()
        for item_id in combined_origin_ids:
            if item_id in seen:
                continue
            seen.add(item_id)
            deduped_origin_ids.append(item_id)
        metadata.update({
            "source": "dream_profile" if category == "user_profile" else "dream_cleanup",
            "category": category or current_meta.get("category") or "identity",
            "dream_batch_id": batch_id,
            "managed_by": "dream",
        })
        if deduped_origin_ids:
            metadata["origin_memory_ids"] = deduped_origin_ids
        return metadata

    async def _apply_semantic_plan(
        self,
        plan: dict[str, Any],
        candidates: list[dict[str, Any]],
        batch_id: str,
    ) -> list[dict[str, Any]]:
        if not self.semantic_memory or not self.semantic_memory.enabled:
            return []
        candidate_map = {str(item.get("id")): item for item in candidates if item.get("id")}
        current_memories = await self.semantic_memory.get_all_memories(limit=self.semantic_scan_limit)
        by_text = {
            self._normalize_text(str(item.get("memory") or "")): item
            for item in current_memories
            if str(item.get("memory") or "").strip()
        }

        results: list[dict[str, Any]] = []
        for raw_action in plan.get("mem0_actions", []):
            if not isinstance(raw_action, dict):
                continue
            action = str(raw_action.get("action") or "").lower().strip()
            category = str(raw_action.get("category") or "").strip() or None
            origin_ids = [
                str(item_id)
                for item_id in raw_action.get("origin_ids") or []
                if str(item_id).strip()
            ]

            if action == "add":
                text = str(raw_action.get("text") or "").strip()
                if not text:
                    continue
                normalized = self._normalize_text(text)
                if normalized in by_text:
                    continue
                metadata = self._build_dream_metadata(
                    current=None,
                    batch_id=batch_id,
                    category=category,
                    origin_ids=origin_ids,
                )
                payload = await self.semantic_memory.add_text(text, metadata, role="assistant")
                new_id = None
                if isinstance(payload, dict):
                    items = payload.get("results") or []
                    if items:
                        new_id = items[0].get("id")
                by_text[normalized] = {"id": new_id, "memory": text, "metadata": metadata}
                results.append({
                    "op": "add",
                    "memory_id": new_id,
                    "old_text": None,
                    "new_text": text,
                    "old_metadata": None,
                    "new_metadata": metadata,
                })
                continue

            target_id = str(raw_action.get("id") or "").strip()
            target = candidate_map.get(target_id)
            if not target or not self._is_manageable_memory(target):
                continue

            if action == "update":
                text = str(raw_action.get("text") or "").strip()
                if not text:
                    continue
                metadata = self._build_dream_metadata(
                    current=target,
                    batch_id=batch_id,
                    category=category,
                    origin_ids=origin_ids,
                )
                await self.semantic_memory.update_memory(target_id, text, metadata)
                by_text[self._normalize_text(text)] = {"id": target_id, "memory": text, "metadata": metadata}
                results.append({
                    "op": "update",
                    "memory_id": target_id,
                    "old_text": target.get("memory"),
                    "new_text": text,
                    "old_metadata": self.semantic_memory._flatten_item_metadata(target),
                    "new_metadata": metadata,
                })
                continue

            if action == "delete":
                await self.semantic_memory.delete_memory(target_id)
                results.append({
                    "op": "delete",
                    "memory_id": target_id,
                    "old_text": target.get("memory"),
                    "new_text": None,
                    "old_metadata": self.semantic_memory._flatten_item_metadata(target),
                    "new_metadata": None,
                })
        return results

    async def _run_semantic_housekeeping(
        self,
        batch_id: str,
        batch: list[dict[str, Any]],
        analysis: str,
    ) -> tuple[list[dict[str, Any]], bool]:
        candidates = await self._collect_semantic_candidates(batch)
        if not candidates and not self._is_placeholder_user_file(self.store.read_user()):
            return [], False

        user_changed = False
        semantic_actions: list[dict[str, Any]] = []
        if candidates:
            try:
                response = await self.provider.chat_with_retry(
                    model=self.model,
                    messages=self._build_semantic_cleanup_prompt(analysis, batch, candidates),
                    tools=None,
                    tool_choice=None,
                )
            except Exception:
                logger.exception("Dream semantic cleanup planning failed")
                response = None
            plan = self._extract_json_object(response.content if response else "")
            if plan:
                user_changed = self._write_user_summary(list(plan.get("user_summary") or []))
                semantic_actions = await self._apply_semantic_plan(plan, candidates, batch_id)
        return semantic_actions, user_changed

    @staticmethod
    def _display_id(batch: dict[str, Any]) -> str:
        commit = str(batch.get("git_commit") or "").strip()
        if commit:
            return commit[:8]
        return str(batch.get("batch_id") or "")[:8]

    def list_batches(self, limit: int = 10) -> list[dict[str, Any]]:
        batches = self.store.read_dream_batches()
        if batches:
            return list(reversed(batches[-limit:]))
        return [
            {
                "batch_id": f"legacy-{commit.sha}",
                "git_commit": commit.sha,
                "created_at": commit.timestamp,
                "message": commit.message,
                "mem0_actions": [],
                "changed_files": [],
                "kind": "legacy",
            }
            for commit in self.store.git.log(max_entries=limit)
        ]

    def get_batch(self, identifier: str | None = None) -> dict[str, Any] | None:
        batch = self.store.find_dream_batch(identifier)
        if batch:
            return batch
        if not identifier:
            commits = self.store.git.log(max_entries=1)
            if not commits:
                return None
            commit = commits[0]
            return {
                "batch_id": f"legacy-{commit.sha}",
                "git_commit": commit.sha,
                "created_at": commit.timestamp,
                "message": commit.message,
                "mem0_actions": [],
                "changed_files": [],
                "kind": "legacy",
            }
        result = self.store.git.show_commit_diff(identifier)
        if not result:
            return None
        commit, _ = result
        return {
            "batch_id": f"legacy-{commit.sha}",
            "git_commit": commit.sha,
            "created_at": commit.timestamp,
            "message": commit.message,
            "mem0_actions": [],
            "changed_files": [],
            "kind": "legacy",
        }

    async def restore_batch(self, identifier: str) -> dict[str, Any] | None:
        batch = self.get_batch(identifier)
        if not batch:
            return None

        restore_batch_id = self._new_batch_id()
        revert_actions: list[dict[str, Any]] = []
        if self.semantic_memory and self.semantic_memory.enabled:
            for action in reversed(list(batch.get("mem0_actions") or [])):
                op = str(action.get("op") or "").lower()
                memory_id = str(action.get("memory_id") or "").strip()
                if not memory_id:
                    continue
                if op == "add":
                    await self.semantic_memory.delete_memory(memory_id)
                    revert_actions.append({
                        "op": "delete",
                        "memory_id": memory_id,
                        "old_text": action.get("new_text"),
                        "new_text": None,
                        "old_metadata": action.get("new_metadata"),
                        "new_metadata": None,
                    })
                elif op == "update" and action.get("old_text"):
                    old_metadata = dict(action.get("old_metadata") or {})
                    if old_metadata:
                        old_metadata["dream_batch_id"] = restore_batch_id
                    await self.semantic_memory.update_memory(memory_id, action["old_text"], old_metadata)
                    revert_actions.append({
                        "op": "update",
                        "memory_id": memory_id,
                        "old_text": action.get("new_text"),
                        "new_text": action.get("old_text"),
                        "old_metadata": action.get("new_metadata"),
                        "new_metadata": old_metadata,
                    })
                elif op == "delete" and action.get("old_text"):
                    old_metadata = dict(action.get("old_metadata") or {})
                    if old_metadata:
                        old_metadata["dream_batch_id"] = restore_batch_id
                    await self.semantic_memory.restore_memory(memory_id, action["old_text"], old_metadata)
                    revert_actions.append({
                        "op": "add",
                        "memory_id": memory_id,
                        "old_text": None,
                        "new_text": action.get("old_text"),
                        "old_metadata": None,
                        "new_metadata": old_metadata,
                    })

        new_git_sha = None
        if batch.get("git_commit"):
            new_git_sha = self.store.git.revert(str(batch["git_commit"]))

        if new_git_sha or revert_actions:
            record = {
                "batch_id": restore_batch_id,
                "kind": "restore",
                "created_at": datetime.now().isoformat(),
                "restores_batch_id": batch.get("batch_id"),
                "git_commit": new_git_sha,
                "message": f"restore: undo {self._display_id(batch)}",
                "changed_files": list(batch.get("changed_files") or []),
                "mem0_actions": revert_actions,
                "history_cursor_from": batch.get("history_cursor_from"),
                "history_cursor_to": batch.get("history_cursor_to"),
            }
            self.store.append_dream_batch(record)

        return {
            "batch": batch,
            "new_git_sha": new_git_sha,
            "mem0_actions": revert_actions,
        }

    # -- main entry ----------------------------------------------------------

    async def run(self) -> bool:
        """Process unprocessed history entries. Returns True if work was done."""
        last_cursor = self.store.get_last_dream_cursor()
        entries = self.store.read_unprocessed_history(since_cursor=last_cursor)
        raw_batch = entries[: self.max_batch_size]
        batch = [
            entry for entry in raw_batch
            if not is_non_durable_workflow_content(entry.get("content"))
        ]
        skipped_entries = len(raw_batch) - len(batch)
        if not raw_batch and not (self.semantic_memory and self.semantic_memory.enabled):
            return False

        analysis = "No new history entries. Perform semantic housekeeping only."
        result = None
        changelog: list[str] = []
        new_cursor = last_cursor

        if raw_batch:
            logger.info(
                "Dream: processing {} entries (cursor {}→{}), batch={}",
                len(entries), last_cursor, raw_batch[-1]["cursor"], len(batch),
            )
            if skipped_entries:
                logger.info(
                    "Dream: skipped {} non-durable workflow entr{} in current batch",
                    skipped_entries,
                    "y" if skipped_entries == 1 else "ies",
                )

            if batch:
                history_text = "\n".join(
                    f"[{e['timestamp']}] {e['content']}" for e in batch
                )
                file_context = self._build_file_context()
                phase1_prompt = f"## Conversation History\n{history_text}\n\n{file_context}"

                try:
                    phase1_response = await self.provider.chat_with_retry(
                        model=self.model,
                        messages=[
                            {
                                "role": "system",
                                "content": render_template("agent/dream_phase1.md", strip=True),
                            },
                            {"role": "user", "content": phase1_prompt},
                        ],
                        tools=None,
                        tool_choice=None,
                    )
                    analysis = phase1_response.content or ""
                    logger.debug("Dream Phase 1 complete ({} chars)", len(analysis))
                except Exception:
                    logger.exception("Dream Phase 1 failed")
                    return False

                phase2_prompt = f"## Analysis Result\n{analysis}\n\n{file_context}"
                messages: list[dict[str, Any]] = [
                    {
                        "role": "system",
                        "content": render_template("agent/dream_phase2.md", strip=True),
                    },
                    {"role": "user", "content": phase2_prompt},
                ]

                try:
                    result = await self._runner.run(AgentRunSpec(
                        initial_messages=messages,
                        tools=self._tools,
                        model=self.model,
                        max_iterations=self.max_iterations,
                        max_tool_result_chars=self.max_tool_result_chars,
                        fail_on_tool_error=False,
                    ))
                    logger.debug(
                        "Dream Phase 2 complete: stop_reason={}, tool_events={}",
                        result.stop_reason, len(result.tool_events),
                    )
                except Exception:
                    logger.exception("Dream Phase 2 failed")
                    result = None

                if result and result.tool_events:
                    for event in result.tool_events:
                        if event["status"] == "ok":
                            changelog.append(f"{event['name']}: {event['detail']}")

            new_cursor = raw_batch[-1]["cursor"]
            self.store.set_last_dream_cursor(new_cursor)
            self.store.compact_history()

        batch_id = self._new_batch_id()
        semantic_actions, user_changed = await self._run_semantic_housekeeping(batch_id, batch, analysis)
        if user_changed:
            changelog.append("write_user: USER.md")

        normalized_soul, soul_changed = self._strip_forbidden_soul_sections(self.store.read_soul())
        if soul_changed:
            self.store.write_soul(normalized_soul)
            changelog.append("normalize_soul: SOUL.md")

        git_sha = None
        if changelog and self.store.git.is_initialized():
            ts = raw_batch[-1]["timestamp"] if raw_batch else datetime.now().strftime("%Y-%m-%d %H:%M")
            git_sha = self.store.git.auto_commit(f"dream: {ts}, {len(changelog)} change(s)")
            if git_sha:
                logger.info("Dream commit: {}", git_sha)

        if raw_batch:
            if result and result.stop_reason == "completed":
                logger.info(
                    "Dream done: {} markdown change(s), {} semantic action(s), cursor advanced to {}",
                    len(changelog), len(semantic_actions), new_cursor,
                )
            else:
                reason = result.stop_reason if result else "exception"
                logger.warning(
                    "Dream incomplete ({}): cursor advanced to {}",
                    reason, new_cursor,
                )

        if changelog or semantic_actions:
            self.store.append_dream_batch({
                "batch_id": batch_id,
                "kind": "dream",
                "created_at": datetime.now().isoformat(),
                "git_commit": git_sha,
                "message": f"dream batch {batch_id}",
                "changed_files": sorted({
                    event.split(": ", 1)[1]
                    for event in changelog
                    if ": " in event
                }),
                "mem0_actions": semantic_actions,
                "history_cursor_from": last_cursor,
                "history_cursor_to": new_cursor,
            })

        return bool(raw_batch or semantic_actions or user_changed or soul_changed)


MemoryConsolidator = Consolidator
