"""Telegram channel implementation using python-telegram-bot."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import re
import time
from typing import TYPE_CHECKING, Any
import urllib.error
import urllib.request

from loguru import logger
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import (
    BadRequest,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import TelegramConfig

if TYPE_CHECKING:
    from nanobot.session.manager import SessionManager


def _markdown_to_telegram_html(text: str) -> str:
    """
    Convert markdown to Telegram-safe HTML.
    """
    if not text:
        return ""
    
    # 1. Extract and protect code blocks (preserve content from other processing)
    code_blocks: list[str] = []
    def save_code_block(m: re.Match) -> str:
        code_blocks.append(m.group(1))
        return f"\x00CB{len(code_blocks) - 1}\x00"
    
    text = re.sub(r'```[\w]*\n?([\s\S]*?)```', save_code_block, text)
    
    # 2. Extract and protect inline code
    inline_codes: list[str] = []
    def save_inline_code(m: re.Match) -> str:
        inline_codes.append(m.group(1))
        return f"\x00IC{len(inline_codes) - 1}\x00"
    
    text = re.sub(r'`([^`]+)`', save_inline_code, text)
    
    # 3. Headers # Title -> just the title text
    text = re.sub(r'^#{1,6}\s+(.+)$', r'\1', text, flags=re.MULTILINE)
    
    # 4. Blockquotes > text -> just the text (before HTML escaping)
    text = re.sub(r'^>\s*(.*)$', r'\1', text, flags=re.MULTILINE)
    
    # 5. Escape HTML special characters
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    
    # 6. Links [text](url) - must be before bold/italic to handle nested cases
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)
    
    # 7. Bold **text** or __text__
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)
    
    # 8. Italic _text_ (avoid matching inside words like some_var_name)
    text = re.sub(r'(?<![a-zA-Z0-9])_([^_]+)_(?![a-zA-Z0-9])', r'<i>\1</i>', text)
    
    # 9. Strikethrough ~~text~~
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)
    
    # 10. Bullet lists - item -> • item
    text = re.sub(r'^[-*]\s+', '• ', text, flags=re.MULTILINE)
    
    # 11. Restore inline code with HTML tags
    for i, code in enumerate(inline_codes):
        # Escape HTML in code content
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00IC{i}\x00", f"<code>{escaped}</code>")
    
    # 12. Restore code blocks with HTML tags
    for i, code in enumerate(code_blocks):
        # Escape HTML in code content
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00CB{i}\x00", f"<pre><code>{escaped}</code></pre>")
    
    return text


@dataclass
class PendingSend:
    """Persisted outbound Telegram message waiting for retry."""

    message_id: str
    chat_id: str
    text: str
    parse_mode: str | None
    created_at: float
    attempts: int
    next_retry_at: float
    last_error: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "chat_id": self.chat_id,
            "text": self.text,
            "parse_mode": self.parse_mode,
            "created_at": self.created_at,
            "attempts": self.attempts,
            "next_retry_at": self.next_retry_at,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PendingSend":
        now = time.time()
        return cls(
            message_id=str(raw.get("message_id") or f"legacy-{int(now * 1000)}"),
            chat_id=str(raw.get("chat_id") or ""),
            text=str(raw.get("text") or ""),
            parse_mode=(str(raw.get("parse_mode")) if raw.get("parse_mode") is not None else None),
            created_at=float(raw.get("created_at") or now),
            attempts=int(raw.get("attempts") or 0),
            next_retry_at=float(raw.get("next_retry_at") or now),
            last_error=str(raw.get("last_error") or ""),
        )


class TelegramChannel(BaseChannel):
    """
    Telegram channel using long polling.
    
    Simple and reliable - no webhook/public IP needed.
    """
    
    name = "telegram"
    
    # Commands registered with Telegram's command menu
    BOT_COMMANDS = [
        BotCommand("start", "Start the bot"),
        BotCommand("reset", "Reset conversation history"),
        BotCommand("help", "Show available commands"),
        BotCommand("plan_reply", "Reply to plan questions"),
        BotCommand("plan_run", "Run a ready plan"),
        BotCommand("plan_cancel", "Cancel plan execution"),
        BotCommand("task_status", "Query Codex task status"),
    ]
    
    def __init__(
        self,
        config: TelegramConfig,
        bus: MessageBus,
        groq_api_key: str = "",
        session_manager: SessionManager | None = None,
    ):
        super().__init__(config, bus)
        self.config: TelegramConfig = config
        self.groq_api_key = groq_api_key
        self.session_manager = session_manager
        self._app: Application | None = None
        self._chat_ids: dict[str, int] = {}  # Map sender_id to chat_id for replies
        self._typing_tasks: dict[str, asyncio.Task] = {}  # chat_id -> typing loop task
        self._pending_exec_confirms: set[tuple[str, str]] = set()  # (chat_id, task_id)
        self._workspace_path = str(Path.home() / ".nanobot" / "workspace")
        self._outbox_path = Path(self.config.send_retry_outbox_path).expanduser()
        self._outbox: list[PendingSend] = []
        self._outbox_lock = asyncio.Lock()
        self._retry_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._api_available: bool | None = None
        self._dropped_counts_by_chat: dict[str, int] = {}
        self._send_seq = 0
    
    async def start(self) -> None:
        """Start the Telegram bot with long polling."""
        if not self.config.token:
            logger.error("Telegram bot token not configured")
            return
        
        self._running = True
        
        # Build the application with larger connection pool; configure proxy on request object.
        req_kwargs: dict[str, Any] = {
            "connection_pool_size": 16,
            "pool_timeout": 5.0,
            "connect_timeout": 30.0,
            "read_timeout": 30.0,
        }
        if self.config.proxy:
            req_kwargs["proxy"] = self.config.proxy
        req = HTTPXRequest(**req_kwargs)
        builder = Application.builder().token(self.config.token).request(req).get_updates_request(req)
        self._app = builder.build()
        self._app.add_error_handler(self._on_error)
        
        # Add command handlers
        self._app.add_handler(CommandHandler("start", self._on_start))
        self._app.add_handler(CommandHandler("reset", self._on_reset))
        self._app.add_handler(CommandHandler("help", self._on_help))
        self._app.add_handler(CommandHandler("plan_reply", self._on_plan_reply))
        self._app.add_handler(CommandHandler("plan_run", self._on_plan_run))
        self._app.add_handler(CommandHandler("plan_cancel", self._on_plan_cancel))
        self._app.add_handler(CommandHandler("task_status", self._on_task_status))
        self._app.add_handler(CallbackQueryHandler(self._on_callback_query))

        # Add message handler for text, photos, voice, documents
        self._app.add_handler(
            MessageHandler(
                (filters.TEXT | filters.PHOTO | filters.VOICE | filters.AUDIO | filters.Document.ALL),
                self._on_message
            )
        )
        
        logger.info("Starting Telegram bot (polling mode)...")
        
        # Initialize and start polling
        await self._app.initialize()
        await self._app.start()
        
        # Get bot info and register command menu
        bot_info = await self._app.bot.get_me()
        logger.info(f"Telegram bot @{bot_info.username} connected")
        
        try:
            await self._app.bot.set_my_commands(self.BOT_COMMANDS)
            logger.debug("Telegram bot commands registered")
        except Exception as e:
            logger.warning(f"Failed to register bot commands: {e}")
        
        # Start polling (this runs until stopped)
        await self._app.updater.start_polling(
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=self.config.drop_pending_updates
        )

        if self.config.send_retry_enabled:
            await self._load_outbox()
            self._retry_task = asyncio.create_task(self._retry_worker(), name="telegram-retry-worker")
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_worker(),
                name="telegram-heartbeat-worker",
            )
            logger.info(
                "Telegram retry enabled "
                f"(queue={len(self._outbox)}, heartbeat={self.config.send_retry_heartbeat_seconds}s)"
            )
        
        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)
    
    async def stop(self) -> None:
        """Stop the Telegram bot."""
        self._running = False

        await self._stop_background_task(self._retry_task)
        self._retry_task = None
        await self._stop_background_task(self._heartbeat_task)
        self._heartbeat_task = None

        if self.config.send_retry_enabled:
            await self._save_outbox()
        
        # Cancel all typing indicators
        for chat_id in list(self._typing_tasks):
            self._stop_typing(chat_id)
        
        if self._app:
            logger.info("Stopping Telegram bot...")
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            self._app = None
    
    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Telegram."""
        if not self._app:
            logger.warning("Telegram bot not running")
            return
        
        # Stop typing indicator for this chat
        self._stop_typing(msg.chat_id)
        text_content = (msg.content or "").strip()
        if not text_content:
            text_content = "抱歉，这次回复为空。请再发一次，我马上重试。"

        try:
            chat_id = str(int(msg.chat_id))
        except ValueError:
            logger.error(f"Invalid chat_id: {msg.chat_id}")
            return

        message_id = self._new_message_id(chat_id)
        try:
            await self._send_message_with_fallback(chat_id, text_content)
            self._api_available = True
            return
        except Exception as e:
            retryable = self._is_retryable_error(e)
            if retryable:
                self._api_available = False
            if not self.config.send_retry_enabled or not retryable:
                logger.error(
                    "Error sending Telegram message (not queued): "
                    f"message_id={message_id}, chat_id={chat_id}, retryable={retryable}, error={e}"
                )
                return

            await self._enqueue_pending(
                PendingSend(
                    message_id=message_id,
                    chat_id=chat_id,
                    text=text_content,
                    parse_mode="HTML",
                    created_at=time.time(),
                    attempts=0,
                    next_retry_at=time.time() + max(1, int(self.config.send_retry_initial_seconds)),
                    last_error=str(e),
                )
            )
            logger.warning(
                "queued_for_retry "
                f"message_id={message_id}, chat_id={chat_id}, error={e}"
            )

    async def _stop_background_task(self, task: asyncio.Task | None) -> None:
        """Cancel and await a background task safely."""
        if not task:
            return
        if task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Background task stopped with error: {e}")

    async def _send_message_with_fallback(self, chat_id: str, text: str) -> None:
        """Try HTML first, then fallback to plain text."""
        if not self._app:
            raise RuntimeError("Telegram app not running")
        html_content = _markdown_to_telegram_html(text)
        try:
            await self._app.bot.send_message(
                chat_id=int(chat_id),
                text=html_content,
                parse_mode="HTML",
            )
            return
        except Exception as html_error:
            logger.warning(
                "HTML parse failed, falling back to plain text: "
                f"chat_id={chat_id}, error={html_error}"
            )
        await self._app.bot.send_message(chat_id=int(chat_id), text=text)

    def _new_message_id(self, chat_id: str) -> str:
        """Generate a local unique message id for outbox tracking."""
        self._send_seq += 1
        return f"{int(time.time() * 1000)}-{chat_id}-{self._send_seq}"

    def _is_retryable_error(self, err: Exception) -> bool:
        """Decide whether a send error should enter retry queue."""
        if isinstance(err, (TimedOut, NetworkError, RetryAfter, urllib.error.URLError)):
            return True
        if isinstance(err, (BadRequest, Forbidden, InvalidToken)):
            return False
        if isinstance(err, TelegramError):
            text = str(err).lower()
            retry_hints = (
                "timed out",
                "timeout",
                "temporarily unavailable",
                "service unavailable",
                "internal server error",
                "bad gateway",
                "gateway timeout",
                "too many requests",
            )
            return any(hint in text for hint in retry_hints)
        return False

    def _retry_after_seconds(self, err: Exception) -> float | None:
        """Extract retry-after seconds when provider asks for cooldown."""
        if not isinstance(err, RetryAfter):
            return None
        raw = err.retry_after
        if hasattr(raw, "total_seconds"):
            try:
                return max(1.0, float(raw.total_seconds()))
            except Exception:
                return None
        try:
            return max(1.0, float(raw))
        except Exception:
            return None

    async def _enqueue_pending(self, pending: PendingSend) -> None:
        """Append outbound message to retry queue and persist to disk."""
        dropped_chat_id: str | None = None
        async with self._outbox_lock:
            self._outbox.append(pending)
            if len(self._outbox) > max(1, int(self.config.send_retry_max_queue)):
                dropped = self._outbox.pop(0)
                dropped_chat_id = dropped.chat_id
                self._dropped_counts_by_chat[dropped.chat_id] = self._dropped_counts_by_chat.get(dropped.chat_id, 0) + 1
            await self._save_outbox_locked()
        if dropped_chat_id:
            logger.warning(
                "retry queue overflow, dropped oldest message "
                f"chat_id={dropped_chat_id}, limit={self.config.send_retry_max_queue}"
            )

    async def _load_outbox(self) -> None:
        """Load persisted retry queue from disk."""
        path = self._outbox_path
        if not path.exists():
            return
        try:
            raw_text = await asyncio.to_thread(path.read_text, encoding="utf-8")
            raw_data = json.loads(raw_text)
            if not isinstance(raw_data, list):
                raise ValueError("outbox file must be a JSON list")
            loaded: list[PendingSend] = []
            for item in raw_data:
                if isinstance(item, dict):
                    loaded.append(PendingSend.from_dict(item))
            async with self._outbox_lock:
                self._outbox = loaded
            logger.info(f"Loaded Telegram retry outbox: {len(loaded)} message(s)")
        except Exception as e:
            logger.error(f"Failed to load Telegram outbox {path}: {e}")

    async def _save_outbox(self) -> None:
        """Persist retry queue to disk."""
        async with self._outbox_lock:
            await self._save_outbox_locked()

    async def _save_outbox_locked(self) -> None:
        """Persist retry queue to disk; caller must hold _outbox_lock."""
        path = self._outbox_path
        payload = [item.to_dict() for item in self._outbox]

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            tmp.replace(path)

        await asyncio.to_thread(_write)

    def _is_expired(self, pending: PendingSend, now_ts: float) -> bool:
        """Check whether queued message exceeded TTL."""
        ttl = max(1, int(self.config.send_retry_ttl_seconds))
        return (now_ts - pending.created_at) > ttl

    async def _drop_expired_locked(self, now_ts: float) -> int:
        """Drop expired pending messages; caller must hold _outbox_lock."""
        keep: list[PendingSend] = []
        dropped_total = 0
        for pending in self._outbox:
            if self._is_expired(pending, now_ts):
                dropped_total += 1
                self._dropped_counts_by_chat[pending.chat_id] = self._dropped_counts_by_chat.get(pending.chat_id, 0) + 1
            else:
                keep.append(pending)
        if dropped_total:
            self._outbox = keep
            await self._save_outbox_locked()
        return dropped_total

    async def _flush_outbox(self, *, force: bool) -> None:
        """Retry queued messages, optionally ignoring next_retry_at."""
        if not self._app:
            return
        async with self._outbox_lock:
            now_ts = time.time()
            dropped = await self._drop_expired_locked(now_ts)
            if dropped:
                logger.warning(f"Dropped {dropped} expired Telegram message(s) from retry outbox")
            due = [
                item
                for item in self._outbox
                if force or item.next_retry_at <= now_ts
            ]

        if not due:
            if self._api_available:
                await self._send_expired_summary_if_needed()
            return

        for pending in due:
            if not self._running:
                return
            try:
                await self._send_message_with_fallback(pending.chat_id, pending.text)
                self._api_available = True
                removed = await self._remove_pending(pending.message_id)
                if removed:
                    logger.info(
                        "retry_success "
                        f"message_id={pending.message_id}, chat_id={pending.chat_id}, attempts={pending.attempts}"
                    )
            except Exception as e:
                retryable = self._is_retryable_error(e)
                if retryable:
                    self._api_available = False
                handled = await self._reschedule_or_drop_pending(
                    message_id=pending.message_id,
                    error=e,
                    retryable=retryable,
                )
                if handled:
                    logger.warning(
                        "retry_failed "
                        f"message_id={pending.message_id}, chat_id={pending.chat_id}, retryable={retryable}, error={e}"
                    )

        if self._api_available:
            await self._send_expired_summary_if_needed()

    async def _remove_pending(self, message_id: str) -> bool:
        """Remove one pending message by message_id."""
        async with self._outbox_lock:
            original_len = len(self._outbox)
            self._outbox = [item for item in self._outbox if item.message_id != message_id]
            if len(self._outbox) == original_len:
                return False
            await self._save_outbox_locked()
            return True

    async def _reschedule_or_drop_pending(self, message_id: str, error: Exception, retryable: bool) -> bool:
        """Update retry schedule (or drop) for one message id."""
        async with self._outbox_lock:
            now_ts = time.time()
            target: PendingSend | None = None
            for item in self._outbox:
                if item.message_id == message_id:
                    target = item
                    break
            if target is None:
                return False

            if self._is_expired(target, now_ts):
                self._outbox = [item for item in self._outbox if item.message_id != message_id]
                self._dropped_counts_by_chat[target.chat_id] = self._dropped_counts_by_chat.get(target.chat_id, 0) + 1
                await self._save_outbox_locked()
                return True

            if not retryable:
                self._outbox = [item for item in self._outbox if item.message_id != message_id]
                await self._save_outbox_locked()
                return True

            target.attempts += 1
            initial = max(1, int(self.config.send_retry_initial_seconds))
            max_wait = max(initial, int(self.config.send_retry_max_seconds))
            delay = min(initial * (2 ** max(target.attempts - 1, 0)), max_wait)
            retry_after = self._retry_after_seconds(error)
            if retry_after is not None:
                delay = max(delay, retry_after)
            target.next_retry_at = now_ts + delay
            target.last_error = str(error)
            await self._save_outbox_locked()
            return True

    async def _send_expired_summary_if_needed(self) -> None:
        """Send one recovery summary per chat for TTL-expired dropped messages."""
        if not self._app or not self._dropped_counts_by_chat:
            return
        sent_chat_ids: list[str] = []
        for chat_id, count in list(self._dropped_counts_by_chat.items()):
            if count <= 0:
                sent_chat_ids.append(chat_id)
                continue
            text = f"离线期间有 {count} 条回复超过24小时未送达，已丢弃。"
            try:
                await self._app.bot.send_message(chat_id=int(chat_id), text=text)
                sent_chat_ids.append(chat_id)
                logger.info(
                    "expired_dropped_summary_sent "
                    f"chat_id={chat_id}, dropped_count={count}"
                )
            except Exception as e:
                if self._is_retryable_error(e):
                    self._api_available = False
                    logger.warning(f"Failed to send dropped summary (will retry later): {e}")
                    return
                logger.warning(f"Failed to send dropped summary (discarded): {e}")
                sent_chat_ids.append(chat_id)
        for chat_id in sent_chat_ids:
            self._dropped_counts_by_chat.pop(chat_id, None)

    async def _check_telegram_api(self) -> bool:
        """Probe Telegram API health using get_me."""
        if not self._app:
            return False
        try:
            await self._app.bot.get_me()
            return True
        except Exception:
            return False

    async def _heartbeat_worker(self) -> None:
        """Periodically probe Telegram API and flush queue on recovery."""
        interval = max(1, int(self.config.send_retry_heartbeat_seconds))
        while self._running:
            is_up = await self._check_telegram_api()
            prev = self._api_available
            self._api_available = is_up
            if is_up and prev is False:
                logger.info("heartbeat_up")
                await self._flush_outbox(force=True)
            elif not is_up and prev is not False:
                logger.warning("heartbeat_down")
            await asyncio.sleep(interval)

    async def _retry_worker(self) -> None:
        """Retry queued outbound messages based on retry schedule."""
        while self._running:
            try:
                await self._flush_outbox(force=False)
            except Exception as e:
                logger.error(f"Retry worker error: {e}")
            await asyncio.sleep(1)

    async def _on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        if not update.message or not update.effective_user:
            return
        
        user = update.effective_user
        await update.message.reply_text(
            f"👋 Hi {user.first_name}! I'm nanobot.\n\n"
            "Send me a message and I'll respond!\n"
            "Type /help to see available commands."
        )
    
    async def _on_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /reset command — clear conversation history."""
        if not update.message or not update.effective_user:
            return
        
        chat_id = str(update.message.chat_id)
        session_key = f"{self.name}:{chat_id}"
        
        if self.session_manager is None:
            logger.warning("/reset called but session_manager is not available")
            await update.message.reply_text("⚠️ Session management is not available.")
            return
        
        session = self.session_manager.get_or_create(session_key)
        msg_count = len(session.messages)
        session.clear()
        self.session_manager.save(session)
        
        logger.info(f"Session reset for {session_key} (cleared {msg_count} messages)")
        await update.message.reply_text("🔄 Conversation history cleared. Let's start fresh!")
    
    async def _on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help command — show available commands."""
        if not update.message:
            return
        
        help_text = (
            "🐈 <b>nanobot commands</b>\n\n"
            "/start — Start the bot\n"
            "/reset — Reset conversation history\n"
            "/help — Show this help message\n"
            "/plan_reply & /plan-reply — Reply to PlanBridge questions\n"
            "/plan_run & /plan-run — Execute a ready plan\n"
            "/plan_cancel & /plan-cancel — Cancel a pending plan run\n"
            "/task_status & /task-status — Query task status\n\n"
            "Just send me a text message to chat!"
        )
        await update.message.reply_text(help_text, parse_mode="HTML")

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming messages (text, photos, voice, documents)."""
        if not update.message or not update.effective_user:
            return
        
        message = update.message
        user = update.effective_user
        chat_id = message.chat_id
        
        # Use stable numeric ID, but keep username for allowlist compatibility
        sender_id = str(user.id)
        if user.username:
            sender_id = f"{sender_id}|{user.username}"
        
        # Store chat_id for replies
        self._chat_ids[sender_id] = chat_id

        # Handle hyphen-style plan commands that may not be parsed by Telegram command menu.
        if message.text:
            if await self._maybe_handle_plan_text_command(
                update=update,
                sender_id=sender_id,
                text=message.text,
            ):
                return

            # Optional auto-bind for plain text replies when exactly one needs_input task is open.
            if (
                self.config.plan_bridge_auto_bind_natural_language
                and not message.text.strip().startswith("/")
                and not (message.photo or message.voice or message.audio or message.document)
            ):
                if await self._maybe_auto_bind_plan_reply(
                    update=update,
                    sender_id=sender_id,
                    text=message.text.strip(),
                ):
                    return

        # Build content from text and/or media
        content_parts = []
        media_paths = []
        
        # Text content
        if message.text:
            content_parts.append(message.text)
        if message.caption:
            content_parts.append(message.caption)
        
        # Handle media files
        media_file = None
        media_type = None
        
        if message.photo:
            media_file = message.photo[-1]  # Largest photo
            media_type = "image"
        elif message.voice:
            media_file = message.voice
            media_type = "voice"
        elif message.audio:
            media_file = message.audio
            media_type = "audio"
        elif message.document:
            media_file = message.document
            media_type = "file"
        
        # Download media if present
        if media_file and self._app:
            try:
                file = await self._app.bot.get_file(media_file.file_id)
                ext = self._get_extension(media_type, getattr(media_file, 'mime_type', None))
                
                # Save to workspace/media/
                media_dir = Path.home() / ".nanobot" / "media"
                media_dir.mkdir(parents=True, exist_ok=True)
                
                file_path = media_dir / f"{media_file.file_id[:16]}{ext}"
                await file.download_to_drive(str(file_path))
                
                media_paths.append(str(file_path))
                
                # Handle voice transcription
                if media_type == "voice" or media_type == "audio":
                    from nanobot.providers.transcription import GroqTranscriptionProvider
                    transcriber = GroqTranscriptionProvider(api_key=self.groq_api_key)
                    transcription = await transcriber.transcribe(file_path)
                    if transcription:
                        logger.info(f"Transcribed {media_type}: {transcription[:50]}...")
                        content_parts.append(f"[transcription: {transcription}]")
                    else:
                        content_parts.append(f"[{media_type}: {file_path}]")
                else:
                    content_parts.append(f"[{media_type}: {file_path}]")
                    
                logger.debug(f"Downloaded {media_type} to {file_path}")
            except Exception as e:
                logger.error(f"Failed to download media: {e}")
                content_parts.append(f"[{media_type}: download failed]")
        
        content = "\n".join(content_parts) if content_parts else "[empty message]"
        
        logger.debug(f"Telegram message from {sender_id}: {content[:50]}...")
        
        str_chat_id = str(chat_id)
        
        # Start typing indicator before processing
        self._start_typing(str_chat_id)
        
        # Forward to the message bus
        await self._handle_message(
            sender_id=sender_id,
            chat_id=str_chat_id,
            content=content,
            media=media_paths,
            metadata={
                "message_id": message.message_id,
                "user_id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "is_group": message.chat.type != "private"
            }
        )

    async def _on_plan_reply(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /plan_reply command."""
        if not update.message or not update.effective_user:
            return
        sender_id = self._sender_id(update.effective_user.id, update.effective_user.username)
        if not self.is_allowed(sender_id):
            await update.message.reply_text("未授权用户，无法执行该操作。")
            return
        if len(context.args) < 2:
            await update.message.reply_text("用法：/plan-reply <task_id> <你的回答>")
            return
        task_id = context.args[0].strip()
        answer = " ".join(context.args[1:]).strip()
        await self._submit_plan_reply(update.message.chat_id, task_id, answer)

    async def _on_plan_run(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /plan_run command."""
        if not update.message or not update.effective_user:
            return
        sender_id = self._sender_id(update.effective_user.id, update.effective_user.username)
        if not self.is_allowed(sender_id):
            await update.message.reply_text("未授权用户，无法执行该操作。")
            return
        if len(context.args) < 1:
            await update.message.reply_text("用法：/plan-run <task_id>")
            return
        await self._submit_plan_run(update.message.chat_id, context.args[0].strip())

    async def _on_plan_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /plan_cancel command."""
        if not update.message or not update.effective_user:
            return
        sender_id = self._sender_id(update.effective_user.id, update.effective_user.username)
        if not self.is_allowed(sender_id):
            await update.message.reply_text("未授权用户，无法执行该操作。")
            return
        if len(context.args) < 1:
            await update.message.reply_text("用法：/plan-cancel <task_id>")
            return
        await self._cancel_task(update.message.chat_id, context.args[0].strip())

    async def _on_task_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /task_status command."""
        if not update.message or not update.effective_user:
            return
        sender_id = self._sender_id(update.effective_user.id, update.effective_user.username)
        if not self.is_allowed(sender_id):
            await update.message.reply_text("未授权用户，无法执行该操作。")
            return
        if len(context.args) < 1:
            await update.message.reply_text("用法：/task-status <task_id>")
            return
        task_id = context.args[0].strip()
        try:
            task = await self._listener_get_json(f"/tasks/{task_id}")
        except RuntimeError as e:
            await update.message.reply_text(f"查询失败：{e}")
            return
        await update.message.reply_text(self._format_task_status(task))

    async def _on_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle PlanBridge action buttons from codex-listener messages."""
        if not update.callback_query or not update.effective_user:
            return
        if not self.config.plan_bridge_buttons_enabled:
            return

        query = update.callback_query
        data = query.data or ""
        sender_id = self._sender_id(update.effective_user.id, update.effective_user.username)
        if not self.is_allowed(sender_id):
            await query.answer("未授权", show_alert=True)
            return

        if not data.startswith("pb1|"):
            await query.answer()
            return

        parts = data.split("|", 2)
        if len(parts) != 3:
            await query.answer("无效按钮")
            return
        _, action, task_id = parts
        chat_id = str(query.message.chat_id) if query.message else ""

        await query.answer()

        if action == "exec":
            if self.config.plan_bridge_require_execute_confirm:
                self._pending_exec_confirms.add((chat_id, task_id))
                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("✅ 确认执行", callback_data=f"pb1|exec_confirm|{task_id}"),
                            InlineKeyboardButton("❎ 取消", callback_data=f"pb1|exec_abort|{task_id}"),
                        ]
                    ]
                )
                if query.message:
                    await query.message.reply_text(
                        f"请二次确认是否执行计划（task_id={task_id}）。",
                        reply_markup=keyboard,
                    )
                return
            await self._submit_plan_run(chat_id, task_id)
            return

        if action == "exec_confirm":
            if self.config.plan_bridge_require_execute_confirm and (chat_id, task_id) not in self._pending_exec_confirms:
                if query.message:
                    await query.message.reply_text("确认态已失效，请重新点击“执行计划”。")
                return
            self._pending_exec_confirms.discard((chat_id, task_id))
            await self._submit_plan_run(chat_id, task_id)
            return

        if action == "exec_abort":
            self._pending_exec_confirms.discard((chat_id, task_id))
            if query.message:
                await query.message.reply_text(f"已取消执行（task_id={task_id}）。")
            return

        if action == "cancel":
            self._pending_exec_confirms.discard((chat_id, task_id))
            if query.message:
                await query.message.reply_text(f"已记录取消，不会执行该计划（task_id={task_id}）。")
            return

    async def _maybe_handle_plan_text_command(
        self,
        update: Update,
        sender_id: str,
        text: str,
    ) -> bool:
        """Handle hyphen-style plan commands in plain text."""
        if not update.message:
            return False

        raw = text.strip()
        if not raw.startswith("/"):
            return False

        m = re.match(
            r"^/(plan-reply|plan_run|plan-run|plan_cancel|plan-cancel|task-status|task_status|plan_reply)\b(?:@\w+)?\s*(.*)$",
            raw,
            flags=re.IGNORECASE,
        )
        if not m:
            return False

        if not self.is_allowed(sender_id):
            await update.message.reply_text("未授权用户，无法执行该操作。")
            return True

        cmd = m.group(1).lower().replace("_", "-")
        rest = (m.group(2) or "").strip()

        if cmd == "plan-reply":
            parts = rest.split(maxsplit=1)
            if len(parts) < 2:
                await update.message.reply_text("用法：/plan-reply <task_id> <你的回答>")
                return True
            await self._submit_plan_reply(update.message.chat_id, parts[0].strip(), parts[1].strip())
            return True

        if cmd == "plan-run":
            if not rest:
                await update.message.reply_text("用法：/plan-run <task_id>")
                return True
            await self._submit_plan_run(update.message.chat_id, rest.split(maxsplit=1)[0].strip())
            return True

        if cmd == "plan-cancel":
            if not rest:
                await update.message.reply_text("用法：/plan-cancel <task_id>")
                return True
            await self._cancel_task(update.message.chat_id, rest.split(maxsplit=1)[0].strip())
            return True

        if cmd == "task-status":
            if not rest:
                await update.message.reply_text("用法：/task-status <task_id>")
                return True
            task_id = rest.split(maxsplit=1)[0].strip()
            try:
                task = await self._listener_get_json(f"/tasks/{task_id}")
            except RuntimeError as e:
                await update.message.reply_text(f"查询失败：{e}")
                return True
            await update.message.reply_text(self._format_task_status(task))
            return True

        return False

    async def _maybe_auto_bind_plan_reply(
        self,
        update: Update,
        sender_id: str,
        text: str,
    ) -> bool:
        """Auto-bind plain text reply to exactly one open needs_input task."""
        if not update.message or not text:
            return False
        if not self.is_allowed(sender_id):
            return False

        try:
            open_tasks = await self._list_open_needs_input_tasks()
        except RuntimeError as e:
            logger.warning(f"Auto-bind skipped: {e}")
            return False

        if len(open_tasks) == 0:
            return False
        if len(open_tasks) > 1:
            ids = ", ".join(t["task_id"] for t in open_tasks[:5] if t.get("task_id"))
            await update.message.reply_text(
                "检测到多个待回答 Plan 任务，请使用：/plan-reply <task_id> <你的回答>\n"
                f"候选 task_id: {ids}"
            )
            return True

        task = open_tasks[0]
        task_id = str(task.get("task_id", "")).strip()
        if not task_id:
            return False
        await self._submit_plan_reply(update.message.chat_id, task_id, text)
        return True

    async def _submit_plan_reply(self, chat_id: int | str, task_id: str, answer: str) -> None:
        """Create a plan_bridge child task by replying to needs_input."""
        try:
            parent = await self._listener_get_json(f"/tasks/{task_id}")
        except RuntimeError as e:
            await self._reply_text(chat_id, f"读取任务失败：{e}")
            return

        if parent.get("bridge_stage") != "needs_input":
            await self._reply_text(chat_id, f"任务 {task_id} 当前不是 needs_input 阶段。")
            return
        session_id = str(parent.get("session_id") or "").strip()
        if not session_id:
            await self._reply_text(chat_id, f"任务 {task_id} 缺少 session_id，无法 resume。")
            return

        prompt = (
            "用户已回答上一轮澄清问题。请继续 PlanBridge。\n"
            "要求：如果还需要信息，输出 planmode.v1 needs_input JSON；"
            "如果信息充分，输出 planmode.v1 plan_ready JSON。\n"
            "禁止执行实现。\n\n"
            f"用户回答：{answer}"
        )
        payload = {
            "prompt": prompt,
            "cwd": self._workspace_path,
            "workflow_mode": "plan_bridge",
            "resume_session_id": session_id,
            "parent_task_id": task_id,
        }
        try:
            created = await self._listener_post_json("/tasks", payload)
        except RuntimeError as e:
            await self._reply_text(chat_id, f"提交失败：{e}")
            return
        new_id = created.get("task_id")
        await self._reply_text(chat_id, f"已提交续跑任务：{new_id}")

    async def _submit_plan_run(self, chat_id: int | str, task_id: str) -> None:
        """Create a normal execution task from a plan_ready task."""
        try:
            parent = await self._listener_get_json(f"/tasks/{task_id}")
        except RuntimeError as e:
            await self._reply_text(chat_id, f"读取任务失败：{e}")
            return

        if parent.get("bridge_stage") != "plan_ready":
            await self._reply_text(chat_id, f"任务 {task_id} 当前不是 plan_ready 阶段。")
            return
        plan_md = str(parent.get("bridge_plan") or "").strip()
        if not plan_md:
            await self._reply_text(chat_id, f"任务 {task_id} 没有可执行计划内容。")
            return

        prompt = (
            "执行以下已确认计划。\n"
            "要求：按计划执行；涉及删除/覆盖前先二次确认；"
            "最终输出关键验收结果。\n\n"
            f"{plan_md}"
        )
        payload = {
            "prompt": prompt,
            "cwd": self._workspace_path,
            "workflow_mode": "normal",
            "parent_task_id": task_id,
        }
        try:
            created = await self._listener_post_json("/tasks", payload)
        except RuntimeError as e:
            await self._reply_text(chat_id, f"提交执行任务失败：{e}")
            return
        new_id = created.get("task_id")
        await self._reply_text(chat_id, f"已提交执行任务：{new_id}")

    async def _cancel_task(self, chat_id: int | str, task_id: str) -> None:
        """Cancel a running/pending listener task."""
        try:
            task = await self._listener_delete_json(f"/tasks/{task_id}")
        except RuntimeError as e:
            await self._reply_text(chat_id, f"取消失败：{e}")
            return
        status = task.get("status")
        await self._reply_text(chat_id, f"已处理取消请求：task_id={task_id}, status={status}")

    async def _list_open_needs_input_tasks(self) -> list[dict[str, Any]]:
        """Return unresolved plan_bridge needs_input tasks."""
        tasks = await self._listener_get_json("/tasks")
        if not isinstance(tasks, list):
            return []
        referenced: set[str] = set()
        for t in tasks:
            if isinstance(t, dict):
                parent = t.get("parent_task_id")
                if isinstance(parent, str) and parent.strip():
                    referenced.add(parent.strip())
        result: list[dict[str, Any]] = []
        for t in tasks:
            if not isinstance(t, dict):
                continue
            task_id = str(t.get("task_id") or "").strip()
            if not task_id:
                continue
            if t.get("bridge_stage") != "needs_input":
                continue
            if task_id in referenced:
                continue
            if str(t.get("workflow_mode") or "") != "plan_bridge":
                continue
            if not str(t.get("session_id") or "").strip():
                continue
            result.append(t)
        return result

    def _sender_id(self, user_id: int, username: str | None) -> str:
        """Build sender id with username compatibility."""
        if username:
            return f"{user_id}|{username}"
        return str(user_id)

    async def _reply_text(self, chat_id: int | str, text: str) -> None:
        """Reply to chat with a plain text message."""
        if not self._app:
            return
        try:
            await self._app.bot.send_message(chat_id=int(chat_id), text=text)
        except Exception as e:
            logger.warning(f"Reply failed: {e}")

    def _listener_url(self, path: str) -> str:
        """Build listener API URL."""
        base = self.config.codex_listener_base_url.rstrip("/")
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{base}{path}"

    async def _listener_get_json(self, path: str) -> Any:
        return await self._listener_request_json("GET", path, None)

    async def _listener_post_json(self, path: str, payload: dict[str, Any]) -> Any:
        return await self._listener_request_json("POST", path, payload)

    async def _listener_delete_json(self, path: str) -> Any:
        return await self._listener_request_json("DELETE", path, None)

    async def _listener_request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
    ) -> Any:
        """HTTP JSON request to codex-listener."""
        url = self._listener_url(path)

        def _request() -> Any:
            data = None
            headers: dict[str, str] = {}
            if payload is not None:
                data = json.dumps(payload).encode("utf-8")
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                if not raw:
                    return {}
                return json.loads(raw)

        try:
            return await asyncio.to_thread(_request)
        except urllib.error.HTTPError as e:
            detail = e.reason
            try:
                body = e.read().decode("utf-8", errors="replace")
                parsed = json.loads(body)
                if isinstance(parsed, dict) and parsed.get("detail"):
                    detail = parsed.get("detail")
            except Exception:
                pass
            raise RuntimeError(f"HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            raise RuntimeError(str(e)) from e

    def _format_task_status(self, task: dict[str, Any]) -> str:
        """Build a compact task status summary."""
        task_id = task.get("task_id")
        status = task.get("status")
        bridge = task.get("bridge_stage")
        session_id = task.get("session_id")
        return (
            f"task_id={task_id}\n"
            f"status={status}\n"
            f"bridge_stage={bridge}\n"
            f"session_id={session_id}"
        )
    
    def _start_typing(self, chat_id: str) -> None:
        """Start sending 'typing...' indicator for a chat."""
        # Cancel any existing typing task for this chat
        self._stop_typing(chat_id)
        self._typing_tasks[chat_id] = asyncio.create_task(self._typing_loop(chat_id))
    
    def _stop_typing(self, chat_id: str) -> None:
        """Stop the typing indicator for a chat."""
        task = self._typing_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()
    
    async def _typing_loop(self, chat_id: str) -> None:
        """Repeatedly send 'typing' action until cancelled."""
        try:
            while self._app:
                await self._app.bot.send_chat_action(chat_id=int(chat_id), action="typing")
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"Typing indicator stopped for {chat_id}: {e}")
    
    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log polling / handler errors instead of silently swallowing them."""
        logger.error(f"Telegram error: {context.error}")

    def _get_extension(self, media_type: str, mime_type: str | None) -> str:
        """Get file extension based on media type."""
        if mime_type:
            ext_map = {
                "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
            }
            if mime_type in ext_map:
                return ext_map[mime_type]
        
        type_map = {"image": ".jpg", "voice": ".ogg", "audio": ".mp3", "file": ""}
        return type_map.get(media_type, "")
