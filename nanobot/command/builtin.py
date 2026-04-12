"""Built-in slash command handlers."""

from __future__ import annotations

import asyncio
import os
import sys

from nanobot import __version__
from nanobot.bus.events import OutboundMessage
from nanobot.command.router import CommandContext, CommandRouter
from nanobot.utils.helpers import build_status_content
from nanobot.utils.restart import set_restart_notice_to_env


async def cmd_stop(ctx: CommandContext) -> OutboundMessage:
    """Cancel all active tasks and subagents for the session."""
    loop = ctx.loop
    msg = ctx.msg
    tasks = loop._active_tasks.pop(msg.session_key, [])
    cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
    for t in tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    sub_cancelled = await loop.subagents.cancel_by_session(msg.session_key)
    total = cancelled + sub_cancelled
    content = f"Stopped {total} task(s)." if total else "No active task to stop."
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content=content,
        metadata=dict(msg.metadata or {})
    )


async def cmd_restart(ctx: CommandContext) -> OutboundMessage:
    """Restart the process in-place via os.execv."""
    msg = ctx.msg
    set_restart_notice_to_env(channel=msg.channel, chat_id=msg.chat_id)

    async def _do_restart():
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable, "-m", "nanobot"] + sys.argv[1:])

    asyncio.create_task(_do_restart())
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content="Restarting...",
        metadata=dict(msg.metadata or {})
    )


async def cmd_status(ctx: CommandContext) -> OutboundMessage:
    """Build an outbound status message for a session."""
    loop = ctx.loop
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    effective_model = loop.get_effective_model(ctx.key)
    ctx_est = 0
    try:
        ctx_est, _ = loop.consolidator.estimate_session_prompt_tokens(session)
    except Exception:
        pass
    if ctx_est <= 0:
        ctx_est = loop._last_usage.get("prompt_tokens", 0)
    
    # Fetch web search provider usage (best-effort, never blocks the response)
    search_usage_text: str | None = None
    try:
        from nanobot.utils.searchusage import fetch_search_usage
        web_cfg = getattr(loop, "web_config", None)
        search_cfg = getattr(web_cfg, "search", None) if web_cfg else None
        if search_cfg is not None:
            provider = getattr(search_cfg, "provider", "duckduckgo")
            api_key = getattr(search_cfg, "api_key", "") or None
            usage = await fetch_search_usage(provider=provider, api_key=api_key)
            search_usage_text = usage.format()
    except Exception:
        pass  # Never let usage fetch break /status
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=build_status_content(
            version=__version__, model=effective_model, default_model=loop.default_model,
            start_time=loop._start_time, last_usage=loop._last_usage,
            context_window_tokens=loop.context_window_tokens,
            session_msg_count=len(session.get_history(max_messages=0)),
            context_tokens_estimate=ctx_est,
            search_usage_text=search_usage_text,
        ),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_new(ctx: CommandContext) -> OutboundMessage:
    """Start a fresh session."""
    loop = ctx.loop
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    snapshot = session.messages[session.last_consolidated:]
    session.clear()
    loop.clear_session_model_override(ctx.key)
    loop.sessions.save(session)
    loop.sessions.invalidate(session.key)
    if snapshot:
        loop._schedule_background(loop.consolidator.archive(snapshot))
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content="New session started.",
        metadata=dict(ctx.msg.metadata or {})
    )


async def cmd_dream(ctx: CommandContext) -> OutboundMessage:
    """Manually trigger a Dream consolidation run."""
    import time

    loop = ctx.loop
    msg = ctx.msg

    async def _run_dream():
        t0 = time.monotonic()
        try:
            did_work = await loop.dream.run()
            elapsed = time.monotonic() - t0
            if did_work:
                content = f"Dream completed in {elapsed:.1f}s."
            else:
                content = "Dream: nothing to process."
        except Exception as e:
            elapsed = time.monotonic() - t0
            content = f"Dream failed after {elapsed:.1f}s: {e}"
        await loop.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
        ))

    asyncio.create_task(_run_dream())
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content="Dreaming...",
    )


def _extract_changed_files(diff: str) -> list[str]:
    """Extract changed file paths from a unified diff."""
    files: list[str] = []
    seen: set[str] = set()
    for line in diff.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        path = parts[3]
        if path.startswith("b/"):
            path = path[2:]
        if path in seen:
            continue
        seen.add(path)
        files.append(path)
    return files


def _format_changed_files(diff: str) -> str:
    files = _extract_changed_files(diff)
    if not files:
        return "No tracked memory files changed."
    return ", ".join(f"`{path}`" for path in files)


def _summarize_mem0_actions(actions: list[dict] | None) -> str:
    if not actions:
        return "No semantic memory changes."
    counts = {"add": 0, "update": 0, "delete": 0}
    for action in actions:
        op = str(action.get("op") or "").lower()
        if op in counts:
            counts[op] += 1
    parts = []
    if counts["add"]:
        parts.append(f"{counts['add']} added")
    if counts["update"]:
        parts.append(f"{counts['update']} updated")
    if counts["delete"]:
        parts.append(f"{counts['delete']} deleted")
    return ", ".join(parts) if parts else "No semantic memory changes."


def _format_mem0_action_lines(actions: list[dict] | None, *, limit: int = 5) -> list[str]:
    if not actions:
        return []
    lines: list[str] = []
    for action in actions[:limit]:
        op = str(action.get("op") or "").upper()
        memory_id = str(action.get("memory_id") or "")[:8]
        text = str(action.get("new_text") or action.get("old_text") or "").strip()
        compact = " ".join(text.split())
        if len(compact) > 120:
            compact = compact[:117].rstrip() + "..."
        suffix = f" `{memory_id}`" if memory_id else ""
        if compact:
            lines.append(f"- {op}{suffix}: {compact}")
        else:
            lines.append(f"- {op}{suffix}")
    remaining = len(actions) - len(lines)
    if remaining > 0:
        lines.append(f"- ... and {remaining} more semantic action(s)")
    return lines


def _format_dream_log_content(commit, diff: str, *, requested_sha: str | None = None) -> str:
    files_line = _format_changed_files(diff)
    lines = [
        "## Dream Update",
        "",
        "Here is the selected Dream memory change." if requested_sha else "Here is the latest Dream memory change.",
        "",
        f"- Commit: `{commit.sha}`",
        f"- Time: {commit.timestamp}",
        f"- Changed files: {files_line}",
    ]
    if diff:
        lines.extend([
            "",
            f"Use `/dream-restore {commit.sha}` to undo this change.",
            "",
            "```diff",
            diff.rstrip(),
            "```",
        ])
    else:
        lines.extend([
            "",
            "Dream recorded this version, but there is no file diff to display.",
        ])
    return "\n".join(lines)


def _format_dream_batch_log_content(batch: dict, diff: str = "", *, requested_id: str | None = None) -> str:
    files_line = _format_changed_files(diff) if diff else "No tracked memory files changed."
    batch_id = str(batch.get("batch_id") or "")[:8]
    commit = str(batch.get("git_commit") or "")
    display_id = commit[:8] if commit else batch_id
    lines = [
        "## Dream Update",
        "",
        "Here is the selected Dream memory change." if requested_id else "Here is the latest Dream memory change.",
        "",
        f"- Batch: `{batch_id}`" if batch_id else f"- Batch: `{display_id}`",
        f"- Commit: `{commit[:8]}`" if commit else "- Commit: `(semantic-only batch)`",
        f"- Time: {batch.get('created_at') or '(unknown)'}",
        f"- Changed files: {files_line}",
        f"- Semantic memory: {_summarize_mem0_actions(batch.get('mem0_actions'))}",
    ]
    mem0_lines = _format_mem0_action_lines(batch.get("mem0_actions"))
    if mem0_lines:
        lines.extend(["", "### Semantic Memory"] + mem0_lines)
    if diff:
        lines.extend([
            "",
            f"Use `/dream-restore {display_id}` to undo this Dream batch.",
            "",
            "```diff",
            diff.rstrip(),
            "```",
        ])
    elif not mem0_lines:
        lines.extend([
            "",
            "Dream recorded this version, but there is no file diff or semantic memory change to display.",
        ])
    else:
        lines.extend([
            "",
            f"Use `/dream-restore {display_id}` to undo this Dream batch.",
        ])
    return "\n".join(lines)


def _format_dream_restore_list(commits: list) -> str:
    lines = [
        "## Dream Restore",
        "",
        "Choose a Dream memory version to restore. Latest first:",
        "",
    ]
    for c in commits:
        lines.append(f"- `{c.sha}` {c.timestamp} - {c.message.splitlines()[0]}")
    lines.extend([
        "",
        "Preview a version with `/dream-log <sha>` before restoring it.",
        "Restore a version with `/dream-restore <sha>`.",
    ])
    return "\n".join(lines)


def _format_dream_restore_batches(batches: list[dict]) -> str:
    lines = [
        "## Dream Restore",
        "",
        "Choose a Dream batch to restore. Latest first:",
        "",
    ]
    for batch in batches:
        commit = str(batch.get("git_commit") or "")
        batch_id = str(batch.get("batch_id") or "")[:8]
        display = commit[:8] if commit else batch_id
        when = str(batch.get("created_at") or "(unknown)")
        summary = _summarize_mem0_actions(batch.get("mem0_actions"))
        lines.append(f"- `{display}` {when} - {summary}")
    lines.extend([
        "",
        "Preview a batch with `/dream-log <sha>` before restoring it.",
        "Restore a batch with `/dream-restore <sha>`.",
    ])
    return "\n".join(lines)


async def cmd_dream_log(ctx: CommandContext) -> OutboundMessage:
    """Show what the last Dream changed.

    Default: diff of the latest commit (HEAD~1 vs HEAD).
    With /dream-log <sha>: diff of that specific commit.
    """
    store = ctx.loop.consolidator.store
    git = store.git
    dream = getattr(ctx.loop, "dream", None)

    if dream is not None and hasattr(dream, "get_batch"):
        args = ctx.args.strip()
        batch = dream.get_batch(args or None)
        if batch:
            commit = str(batch.get("git_commit") or "")
            diff = ""
            if commit:
                result = git.show_commit_diff(commit)
                if result:
                    _, diff = result
            content = _format_dream_batch_log_content(batch, diff, requested_id=args or None)
            return OutboundMessage(
                channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
                content=content, metadata={"render_as": "text"},
            )

    if not git.is_initialized():
        if store.get_last_dream_cursor() == 0:
            msg = "Dream has not run yet. Run `/dream`, or wait for the next scheduled Dream cycle."
        else:
            msg = "Dream history is not available because memory versioning is not initialized."
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=msg, metadata={"render_as": "text"},
        )

    args = ctx.args.strip()

    if args:
        # Show diff of a specific commit
        sha = args.split()[0]
        result = git.show_commit_diff(sha)
        if not result:
            content = (
                f"Couldn't find Dream change `{sha}`.\n\n"
                "Use `/dream-restore` to list recent versions, "
                "or `/dream-log` to inspect the latest one."
            )
        else:
            commit, diff = result
            content = _format_dream_log_content(commit, diff, requested_sha=sha)
    else:
        # Default: show the latest commit's diff
        commits = git.log(max_entries=1)
        result = git.show_commit_diff(commits[0].sha) if commits else None
        if result:
            commit, diff = result
            content = _format_dream_log_content(commit, diff)
        else:
            content = "Dream memory has no saved versions yet."

    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=content, metadata={"render_as": "text"},
    )


async def cmd_dream_restore(ctx: CommandContext) -> OutboundMessage:
    """Restore memory files from a previous dream commit.

    Usage:
        /dream-restore          — list recent commits
        /dream-restore <sha>    — revert a specific commit
    """
    store = ctx.loop.consolidator.store
    git = store.git
    dream = getattr(ctx.loop, "dream", None)

    if dream is not None and hasattr(dream, "list_batches") and hasattr(dream, "restore_batch"):
        args = ctx.args.strip()
        if not args:
            batches = dream.list_batches(limit=10)
            if not batches:
                content = "Dream memory has no saved versions to restore yet."
            else:
                content = _format_dream_restore_batches(batches)
        else:
            restored = await dream.restore_batch(args.split()[0])
            if restored:
                batch = restored["batch"]
                display = str(batch.get("git_commit") or "")[:8] or str(batch.get("batch_id") or "")[:8]
                new_sha = restored.get("new_git_sha")
                mem0_summary = _summarize_mem0_actions(restored.get("mem0_actions"))
                lines = [
                    f"Restored Dream memory batch `{display}`.",
                    "",
                    f"- New safety commit: `{new_sha}`" if new_sha else "- New safety commit: `(no markdown changes)`",
                    f"- Semantic memory rollback: {mem0_summary}",
                ]
                mem0_lines = _format_mem0_action_lines(restored.get("mem0_actions"))
                if mem0_lines:
                    lines.extend(["", "### Semantic Memory", *mem0_lines])
                if new_sha:
                    lines.extend(["", f"Use `/dream-log {new_sha}` to inspect the restore diff."])
                content = "\n".join(lines)
            else:
                content = (
                    f"Couldn't restore Dream change `{args.split()[0]}`.\n\n"
                    "It may not exist, or it may be the first saved version with no earlier state to restore."
                )
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=content, metadata={"render_as": "text"},
        )

    if not git.is_initialized():
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="Dream history is not available because memory versioning is not initialized.",
        )

    args = ctx.args.strip()
    if not args:
        # Show recent commits for the user to pick
        commits = git.log(max_entries=10)
        if not commits:
            content = "Dream memory has no saved versions to restore yet."
        else:
            content = _format_dream_restore_list(commits)
    else:
        sha = args.split()[0]
        result = git.show_commit_diff(sha)
        changed_files = _format_changed_files(result[1]) if result else "the tracked memory files"
        new_sha = git.revert(sha)
        if new_sha:
            content = (
                f"Restored Dream memory to the state before `{sha}`.\n\n"
                f"- New safety commit: `{new_sha}`\n"
                f"- Restored files: {changed_files}\n\n"
                f"Use `/dream-log {new_sha}` to inspect the restore diff."
            )
        else:
            content = (
                f"Couldn't restore Dream change `{sha}`.\n\n"
                "It may not exist, or it may be the first saved version with no earlier state to restore."
            )
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=content, metadata={"render_as": "text"},
    )


async def cmd_help(ctx: CommandContext) -> OutboundMessage:
    """Return available slash commands."""
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=build_help_text(),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


def build_help_text() -> str:
    """Build canonical help text shared across channels."""
    lines = [
        "🐈 nanobot commands:",
        "/new — Start a new conversation",
        "/switch — Show or change the model for this chat",
        "/stop — Stop the current task",
        "/restart — Restart the bot",
        "/status — Show bot status",
        "/dream — Manually trigger Dream consolidation",
        "/dream-log — Show what the last Dream changed",
        "/dream-restore — Revert memory to a previous state",
        "/help — Show available commands",
    ]
    return "\n".join(lines)


async def cmd_switch(ctx: CommandContext) -> OutboundMessage:
    """Show or change the active model for the current chat session."""
    loop = ctx.loop
    raw_args = (ctx.args or "").strip()
    current_model = loop.get_effective_model(ctx.key)
    default_model = loop.default_model

    if not raw_args:
        content = "\n".join([
            f"Current model: {current_model}",
            f"Default model: {default_model}",
            "Usage: /switch <model-id>",
            "Use /switch default to reset this chat.",
        ])
    elif raw_args.lower() == "default":
        loop.clear_session_model_override(ctx.key)
        content = "\n".join([
            f"Using default model for this chat: {default_model}",
            "Use /switch <model-id> to override it for this chat.",
        ])
    else:
        loop.set_session_model_override(ctx.key, raw_args)
        content = "\n".join([
            f"Switched this chat to model: {raw_args}",
            f"Default model remains: {default_model}",
            "Use /switch default to reset this chat.",
        ])

    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata={"render_as": "text"},
    )


def register_builtin_commands(router: CommandRouter) -> None:
    """Register the default set of slash commands."""
    router.priority("/stop", cmd_stop)
    router.priority("/restart", cmd_restart)
    router.priority("/status", cmd_status)
    router.exact("/new", cmd_new)
    router.exact("/switch", cmd_switch)
    router.exact("/status", cmd_status)
    router.exact("/dream", cmd_dream)
    router.exact("/dream-log", cmd_dream_log)
    router.prefix("/dream-log ", cmd_dream_log)
    router.exact("/dream_log", cmd_dream_log)
    router.prefix("/dream_log ", cmd_dream_log)
    router.exact("/dream-restore", cmd_dream_restore)
    router.prefix("/dream-restore ", cmd_dream_restore)
    router.exact("/dream_restore", cmd_dream_restore)
    router.prefix("/dream_restore ", cmd_dream_restore)
    router.exact("/help", cmd_help)
    router.prefix("/switch ", cmd_switch)
