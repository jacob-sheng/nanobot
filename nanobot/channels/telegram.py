"""Telegram channel implementation using python-telegram-bot."""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.error
import unicodedata
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Literal

from loguru import logger
from pydantic import Field
from telegram import BotCommand, ReactionTypeEmoji, ReplyParameters, Update
from telegram.error import BadRequest, Forbidden, InvalidToken, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import Application, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.command.builtin import build_help_text
from nanobot.config.paths import get_media_dir
from nanobot.config.schema import Base
from nanobot.security.network import validate_url_target
from nanobot.utils.helpers import split_message

TELEGRAM_MAX_MESSAGE_LEN = 4000  # Telegram message character limit
TELEGRAM_REPLY_CONTEXT_MAX_LEN = TELEGRAM_MAX_MESSAGE_LEN  # Max length for reply context in user message


def _strip_md(s: str) -> str:
    """Strip markdown inline formatting from text."""
    s = re.sub(r'\*\*(.+?)\*\*', r'\1', s)
    s = re.sub(r'__(.+?)__', r'\1', s)
    s = re.sub(r'~~(.+?)~~', r'\1', s)
    s = re.sub(r'`([^`]+)`', r'\1', s)
    return s.strip()


def _render_table_box(table_lines: list[str]) -> str:
    """Convert markdown pipe-table to compact aligned text for <pre> display."""

    def dw(s: str) -> int:
        return sum(2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in s)

    rows: list[list[str]] = []
    has_sep = False
    for line in table_lines:
        cells = [_strip_md(c) for c in line.strip().strip('|').split('|')]
        if all(re.match(r'^:?-+:?$', c) for c in cells if c):
            has_sep = True
            continue
        rows.append(cells)
    if not rows or not has_sep:
        return '\n'.join(table_lines)

    ncols = max(len(r) for r in rows)
    for r in rows:
        r.extend([''] * (ncols - len(r)))
    widths = [max(dw(r[c]) for r in rows) for c in range(ncols)]

    def dr(cells: list[str]) -> str:
        return '  '.join(f'{c}{" " * (w - dw(c))}' for c, w in zip(cells, widths))

    out = [dr(rows[0])]
    out.append('  '.join('─' * w for w in widths))
    for row in rows[1:]:
        out.append(dr(row))
    return '\n'.join(out)


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

    # 1.5. Convert markdown tables to box-drawing (reuse code_block placeholders)
    lines = text.split('\n')
    rebuilt: list[str] = []
    li = 0
    while li < len(lines):
        if re.match(r'^\s*\|.+\|', lines[li]):
            tbl: list[str] = []
            while li < len(lines) and re.match(r'^\s*\|.+\|', lines[li]):
                tbl.append(lines[li])
                li += 1
            box = _render_table_box(tbl)
            if box != '\n'.join(tbl):
                code_blocks.append(box)
                rebuilt.append(f"\x00CB{len(code_blocks) - 1}\x00")
            else:
                rebuilt.extend(tbl)
        else:
            rebuilt.append(lines[li])
            li += 1
    text = '\n'.join(rebuilt)

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


_SEND_MAX_RETRIES = 3
_SEND_RETRY_BASE_DELAY = 0.5  # seconds, doubled each retry
_STREAM_EDIT_INTERVAL_DEFAULT = 0.6  # min seconds between edit_message_text calls


@dataclass
class _StreamBuf:
    """Per-chat streaming accumulator for progressive message editing."""
    text: str = ""
    message_id: int | None = None
    last_edit: float = 0.0
    stream_id: str | None = None


@dataclass
class PendingSend:
    """Persisted outbound Telegram message waiting for retry."""

    message_id: str
    chat_id: str
    text: str
    media: list[str]
    parse_mode: str | None
    reply_to_message_id: int | None
    message_thread_id: int | None
    created_at: float
    attempts: int
    next_retry_at: float
    last_error: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "chat_id": self.chat_id,
            "text": self.text,
            "content": self.text,
            "media": self.media,
            "parse_mode": self.parse_mode,
            "reply_to_message_id": self.reply_to_message_id,
            "message_thread_id": self.message_thread_id,
            "created_at": self.created_at,
            "attempts": self.attempts,
            "next_retry_at": self.next_retry_at,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PendingSend":
        now = time.time()
        media = raw.get("media")
        if not isinstance(media, list):
            media = []

        def _parse_optional_int(value: Any) -> int | None:
            if value is None or value == "":
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        return cls(
            message_id=str(raw.get("message_id") or f"legacy-{int(now * 1000)}"),
            chat_id=str(raw.get("chat_id") or ""),
            text=str(raw.get("text") or raw.get("content") or ""),
            media=[str(item) for item in media if str(item).strip()],
            parse_mode=(str(raw.get("parse_mode")) if raw.get("parse_mode") is not None else None),
            reply_to_message_id=_parse_optional_int(raw.get("reply_to_message_id")),
            message_thread_id=_parse_optional_int(raw.get("message_thread_id")),
            created_at=float(raw.get("created_at") or now),
            attempts=int(raw.get("attempts") or 0),
            next_retry_at=float(raw.get("next_retry_at") or now),
            last_error=str(raw.get("last_error") or ""),
        )

    def with_remaining(
        self,
        *,
        text: str | None = None,
        media: list[str] | None = None,
        last_error: str | None = None,
    ) -> "PendingSend":
        """Return a copy containing only the still-unsent envelope."""
        return PendingSend(
            message_id=self.message_id,
            chat_id=self.chat_id,
            text=self.text if text is None else text,
            media=list(self.media if media is None else media),
            parse_mode=self.parse_mode,
            reply_to_message_id=self.reply_to_message_id,
            message_thread_id=self.message_thread_id,
            created_at=self.created_at,
            attempts=self.attempts,
            next_retry_at=self.next_retry_at,
            last_error=self.last_error if last_error is None else last_error,
        )


class TelegramConfig(Base):
    """Telegram channel configuration."""

    enabled: bool = False
    token: str = ""
    allow_from: list[str] = Field(default_factory=list)
    proxy: str | None = None
    reply_to_message: bool = False
    react_emoji: str = "👀"
    group_policy: Literal["open", "mention"] = "mention"
    connection_pool_size: int = 32
    pool_timeout: float = 5.0
    streaming: bool = True
    stream_edit_interval: float = Field(default=_STREAM_EDIT_INTERVAL_DEFAULT, ge=0.1)
    drop_pending_updates: bool = True
    startup_retry_attempts: int = 3
    startup_retry_delay_seconds: int = 5
    send_retry_enabled: bool = True
    send_retry_initial_seconds: int = 5
    send_retry_max_seconds: int = 300
    send_retry_heartbeat_seconds: int = 30
    send_retry_ttl_seconds: int = 86400
    send_retry_outbox_path: str = "~/.nanobot/state/telegram_outbox.json"
    send_retry_max_queue: int = 1000


class TelegramChannel(BaseChannel):
    """
    Telegram channel using long polling.

    Simple and reliable - no webhook/public IP needed.
    """

    name = "telegram"
    display_name = "Telegram"

    # Commands registered with Telegram's command menu
    BOT_COMMANDS = [
        BotCommand("start", "Start the bot"),
        BotCommand("new", "Start a new conversation"),
        BotCommand("switch", "Switch model for this chat"),
        BotCommand("stop", "Stop the current task"),
        BotCommand("restart", "Restart the bot"),
        BotCommand("status", "Show bot status"),
        BotCommand("dream", "Run Dream memory consolidation now"),
        BotCommand("dream_log", "Show the latest Dream memory change"),
        BotCommand("dream_restore", "Restore Dream memory to an earlier version"),
        BotCommand("help", "Show available commands"),
    ]

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return TelegramConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = TelegramConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: TelegramConfig = config
        self._app: Application | None = None
        self._chat_ids: dict[str, int] = {}  # Map sender_id to chat_id for replies
        self._typing_tasks: dict[str, asyncio.Task] = {}  # chat_id -> typing loop task
        self._media_group_buffers: dict[str, dict] = {}
        self._media_group_tasks: dict[str, asyncio.Task] = {}
        self._message_threads: dict[tuple[str, int], int] = {}
        self._bot_user_id: int | None = None
        self._bot_username: str | None = None
        self._stream_bufs: dict[str, _StreamBuf] = {}  # chat_id -> streaming state
        self._outbox_path = Path(self.config.send_retry_outbox_path).expanduser()
        self._outbox: list[PendingSend] = []
        self._outbox_lock = asyncio.Lock()
        self._retry_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._api_available: bool | None = None
        self._dropped_counts_by_chat: dict[str, int] = {}
        self._send_seq = 0

    def is_allowed(self, sender_id: str) -> bool:
        """Preserve Telegram's legacy id|username allowlist matching."""
        if super().is_allowed(sender_id):
            return True

        allow_list = getattr(self.config, "allow_from", [])
        if not allow_list or "*" in allow_list:
            return False

        sender_str = str(sender_id)
        if sender_str.count("|") != 1:
            return False

        sid, username = sender_str.split("|", 1)
        if not sid.isdigit() or not username:
            return False

        return sid in allow_list or username in allow_list

    @staticmethod
    def _normalize_telegram_command(content: str) -> str:
        """Map Telegram-safe command aliases back to canonical nanobot commands."""
        if not content.startswith("/"):
            return content
        if content == "/dream_log" or content.startswith("/dream_log "):
            return content.replace("/dream_log", "/dream-log", 1)
        if content == "/dream_restore" or content.startswith("/dream_restore "):
            return content.replace("/dream_restore", "/dream-restore", 1)
        return content

    async def start(self) -> None:
        """Start the Telegram bot with long polling."""
        if not self.config.token:
            logger.error("Telegram bot token not configured")
            return

        self._running = True
        max_attempts = max(1, int(self.config.startup_retry_attempts))
        delay_seconds = max(1, int(self.config.startup_retry_delay_seconds))
        attempt = 0

        while self._running:
            try:
                await self._startup_once()
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                attempt += 1
                logger.warning(
                    "Telegram startup attempt {}/{} failed: {}",
                    attempt,
                    max_attempts,
                    e,
                )
                await self._shutdown_app_for_retry()
                if not self._running:
                    break
                if attempt >= max_attempts:
                    logger.error(
                        "Telegram startup exhausted {} attempt(s); continuing retry loop in {}s",
                        max_attempts,
                        delay_seconds,
                    )
                    attempt = 0
                await asyncio.sleep(delay_seconds)

    async def _startup_once(self) -> None:
        """Initialize one Telegram polling session."""
        self._app = self._build_application()
        self._register_handlers()

        logger.info("Starting Telegram bot (polling mode)...")

        await self._app.initialize()
        await self._app.start()

        bot_info = await self._app.bot.get_me()
        self._bot_user_id = getattr(bot_info, "id", None)
        self._bot_username = getattr(bot_info, "username", None)
        logger.info("Telegram bot @{} connected", bot_info.username)

        try:
            await self._app.bot.set_my_commands(self.BOT_COMMANDS)
            logger.debug("Telegram bot commands registered")
        except Exception as e:
            logger.warning("Failed to register bot commands: {}", e)

        await self._app.updater.start_polling(
            allowed_updates=["message"],
            drop_pending_updates=self.config.drop_pending_updates,
        )

        if self.config.send_retry_enabled:
            await self._load_outbox()
            self._retry_task = asyncio.create_task(self._retry_worker(), name="telegram-retry-worker")
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_worker(),
                name="telegram-heartbeat-worker",
            )
            logger.info(
                "Telegram retry enabled (queue={}, heartbeat={}s)",
                len(self._outbox),
                self.config.send_retry_heartbeat_seconds,
            )

        while self._running:
            await asyncio.sleep(1)

    def _build_application(self) -> Application:
        """Build a Telegram Application with separate API/polling pools."""
        proxy = self.config.proxy or None
        api_request = HTTPXRequest(
            connection_pool_size=self.config.connection_pool_size,
            pool_timeout=self.config.pool_timeout,
            connect_timeout=30.0,
            read_timeout=30.0,
            proxy=proxy,
        )
        poll_request = HTTPXRequest(
            connection_pool_size=4,
            pool_timeout=self.config.pool_timeout,
            connect_timeout=30.0,
            read_timeout=30.0,
            proxy=proxy,
        )
        builder = (
            Application.builder()
            .token(self.config.token)
            .request(api_request)
            .get_updates_request(poll_request)
        )
        return builder.build()

    def _register_handlers(self) -> None:
        """Attach Telegram handlers to the current application."""
        if not self._app:
            raise RuntimeError("Telegram app not initialized")

        self._app.add_error_handler(self._on_error)

        # Add command handlers (using Regex to support @username suffixes before bot initialization)
        self._app.add_handler(MessageHandler(filters.Regex(r"^/start(?:@\w+)?$"), self._on_start))
        self._app.add_handler(
            MessageHandler(
                filters.Regex(r"^/(new|switch|stop|restart|status|dream)(?:@\w+)?(?:\s+.*)?$"),
                self._forward_command,
            )
        )
        self._app.add_handler(
            MessageHandler(
                filters.Regex(r"^/(dream-log|dream_log|dream-restore|dream_restore)(?:@\w+)?(?:\s+.*)?$"),
                self._forward_command,
            )
        )
        self._app.add_handler(MessageHandler(filters.Regex(r"^/help(?:@\w+)?$"), self._on_help))

        # Add message handler for text, photos, voice, documents, and locations
        self._app.add_handler(
            MessageHandler(
                (filters.TEXT | filters.PHOTO | filters.VOICE | filters.AUDIO | filters.Document.ALL | filters.LOCATION)
                & ~filters.COMMAND,
                self._on_message
            )
        )

    async def _shutdown_app_for_retry(self) -> None:
        """Best-effort cleanup for a partially initialized Telegram app."""
        await self._stop_background_task(self._retry_task)
        self._retry_task = None
        await self._stop_background_task(self._heartbeat_task)
        self._heartbeat_task = None

        app = self._app
        self._app = None
        self._bot_user_id = None
        self._bot_username = None

        if not app:
            return

        updater = getattr(app, "updater", None)
        if updater is not None:
            try:
                await updater.stop()
            except Exception as e:
                logger.debug("Ignoring Telegram updater stop failure during retry cleanup: {}", e)
        try:
            await app.stop()
        except Exception as e:
            logger.debug("Ignoring Telegram app stop failure during retry cleanup: {}", e)
        try:
            await app.shutdown()
        except Exception as e:
            logger.debug("Ignoring Telegram app shutdown failure during retry cleanup: {}", e)

    async def stop(self) -> None:
        """Stop the Telegram bot."""
        self._running = False
        await self._stop_background_task(self._retry_task)
        self._retry_task = None
        await self._stop_background_task(self._heartbeat_task)
        self._heartbeat_task = None

        # Cancel all typing indicators
        for chat_id in list(self._typing_tasks):
            self._stop_typing(chat_id)

        for task in self._media_group_tasks.values():
            task.cancel()
        self._media_group_tasks.clear()
        self._media_group_buffers.clear()

        if self._app:
            logger.info("Stopping Telegram bot...")
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            self._app = None
            self._bot_user_id = None
            self._bot_username = None

    @staticmethod
    def _get_media_type(path: str) -> str:
        """Guess media type from file extension."""
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext in ("jpg", "jpeg", "png", "gif", "webp"):
            return "photo"
        if ext == "ogg":
            return "voice"
        if ext in ("mp3", "m4a", "wav", "aac"):
            return "audio"
        return "document"

    @staticmethod
    def _is_remote_media_url(path: str) -> bool:
        return path.startswith(("http://", "https://"))

    def _resolve_reply_context(
        self,
        chat_id: str,
        *,
        reply_to_message_id: int | None,
        message_thread_id: int | None,
    ) -> tuple[ReplyParameters | None, dict[str, Any], int | None]:
        """Build reply/thread kwargs for an outbound send envelope."""
        if message_thread_id is None and reply_to_message_id is not None:
            message_thread_id = self._message_threads.get((chat_id, reply_to_message_id))

        thread_kwargs: dict[str, Any] = {}
        if message_thread_id is not None:
            thread_kwargs["message_thread_id"] = message_thread_id

        reply_params = None
        if self.config.reply_to_message and reply_to_message_id is not None:
            reply_params = ReplyParameters(
                message_id=reply_to_message_id,
                allow_sending_without_reply=True,
            )

        return reply_params, thread_kwargs, message_thread_id

    def _build_pending_send(
        self,
        *,
        message_id: str,
        chat_id: str,
        text: str,
        media: list[str],
        reply_to_message_id: int | None,
        message_thread_id: int | None,
        last_error: str,
        next_retry_at: float,
    ) -> PendingSend:
        """Create a persisted outbound envelope."""
        return PendingSend(
            message_id=message_id,
            chat_id=chat_id,
            text=text,
            media=list(media),
            parse_mode="HTML" if text else None,
            reply_to_message_id=reply_to_message_id,
            message_thread_id=message_thread_id,
            created_at=time.time(),
            attempts=0,
            next_retry_at=next_retry_at,
            last_error=last_error,
        )

    async def _send_media_item(
        self,
        chat_id: int,
        media_path: str,
        reply_params: ReplyParameters | None,
        thread_kwargs: dict[str, Any],
    ) -> None:
        """Send one media attachment through Telegram."""
        if not self._app:
            raise RuntimeError("Telegram bot not running")

        media_type = self._get_media_type(media_path)
        sender = {
            "photo": self._app.bot.send_photo,
            "voice": self._app.bot.send_voice,
            "audio": self._app.bot.send_audio,
        }.get(media_type, self._app.bot.send_document)
        param = "photo" if media_type == "photo" else media_type if media_type in ("voice", "audio") else "document"

        if self._is_remote_media_url(media_path):
            ok, error = validate_url_target(media_path)
            if not ok:
                raise ValueError(f"unsafe media URL: {error}")
            await self._call_with_retry(
                sender,
                chat_id=chat_id,
                **{param: media_path},
                reply_parameters=reply_params,
                **thread_kwargs,
            )
            return

        with open(media_path, "rb") as f:
            await self._call_with_retry(
                sender,
                chat_id=chat_id,
                **{param: f},
                reply_parameters=reply_params,
                **thread_kwargs,
            )

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Telegram."""
        if not self._app:
            logger.warning("Telegram bot not running")
            return

        meta = msg.metadata or {}
        is_progress = bool(meta.get("_progress", False))
        if not is_progress:
            self._stop_typing(msg.chat_id)
        text_content = (msg.content or "").strip()

        try:
            chat_id_int = int(msg.chat_id)
        except ValueError:
            logger.error("Invalid chat_id: {}", msg.chat_id)
            return
        chat_id = str(chat_id_int)
        reply_to_message_id = meta.get("message_id")
        message_thread_id = meta.get("message_thread_id")
        try:
            reply_to_message_id_int = int(reply_to_message_id) if reply_to_message_id is not None else None
        except (TypeError, ValueError):
            reply_to_message_id_int = None
        try:
            message_thread_id_int = int(message_thread_id) if message_thread_id is not None else None
        except (TypeError, ValueError):
            message_thread_id_int = None

        message_id = self._new_message_id(chat_id)
        pending = self._build_pending_send(
            message_id=message_id,
            chat_id=chat_id,
            text=text_content,
            media=list(msg.media or []),
            reply_to_message_id=reply_to_message_id_int,
            message_thread_id=message_thread_id_int,
            last_error="",
            next_retry_at=time.time() + max(1, int(self.config.send_retry_initial_seconds)),
        )
        try:
            remainder, remainder_error = await self._deliver_pending(pending)
            if remainder is None:
                self._api_available = True
                return

            retryable = bool(remainder_error and self._is_retryable_error(remainder_error))
            if retryable:
                self._api_available = False
            if not self.config.send_retry_enabled or not retryable:
                logger.error(
                    "Telegram send partially failed and was not queued: message_id={}, chat_id={}, retryable={}, error={}",
                    message_id,
                    chat_id,
                    retryable,
                    remainder_error,
                )
                return

            remainder.next_retry_at = time.time() + max(1, int(self.config.send_retry_initial_seconds))
            remainder.last_error = str(remainder_error)
            await self._enqueue_pending(remainder)
            logger.warning("queued_remaining_for_retry message_id={}, chat_id={}, error={}", message_id, chat_id, remainder_error)
            return
        except Exception as e:
            retryable = self._is_retryable_error(e)
            if retryable:
                self._api_available = False
            if not self.config.send_retry_enabled or not retryable:
                logger.error(
                    "Error sending Telegram message (not queued): message_id={}, chat_id={}, retryable={}, error={}",
                    message_id,
                    chat_id,
                    retryable,
                    e,
                )
                return

            pending.last_error = str(e)
            self._api_available = False
            pending.next_retry_at = time.time() + max(1, int(self.config.send_retry_initial_seconds))
            await self._enqueue_pending(pending)
            logger.warning("queued_for_retry message_id={}, chat_id={}, error={}", message_id, chat_id, e)

    async def _stop_background_task(self, task: asyncio.Task | None) -> None:
        """Cancel and await a background task safely."""
        if not task or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("Background task stopped with error: {}", e)

    async def _deliver_pending(self, pending: PendingSend) -> tuple[PendingSend | None, Exception | None]:
        """Deliver one outbound envelope, returning any retry remainder."""
        if not self._app:
            raise RuntimeError("Telegram bot not running")

        chat_id = int(pending.chat_id)
        reply_params, thread_kwargs, message_thread_id = self._resolve_reply_context(
            pending.chat_id,
            reply_to_message_id=pending.reply_to_message_id,
            message_thread_id=pending.message_thread_id,
        )
        pending.message_thread_id = message_thread_id

        sent_any = False
        media_items = list(pending.media or [])
        for idx, media_path in enumerate(media_items):
            try:
                await self._send_media_item(chat_id, media_path, reply_params, thread_kwargs)
                sent_any = True
            except Exception as e:
                filename = media_path.rsplit("/", 1)[-1]
                retryable = self._is_retryable_error(e)
                logger.error("Failed to send media {}: {}", media_path, e)
                if retryable:
                    if sent_any:
                        return pending.with_remaining(
                            media=media_items[idx:],
                            text=pending.text,
                            last_error=str(e),
                        ), e
                    raise
                try:
                    await self._call_with_retry(
                        self._app.bot.send_message,
                        chat_id=chat_id,
                        text=f"[Failed to send: {filename}]",
                        reply_parameters=reply_params,
                        **thread_kwargs,
                    )
                    sent_any = True
                except Exception as marker_error:
                    logger.warning("Failed to send media failure marker for {}: {}", media_path, marker_error)
                    if self._is_retryable_error(marker_error):
                        if sent_any:
                            return pending.with_remaining(
                                media=media_items[idx:],
                                text=pending.text,
                                last_error=str(marker_error),
                            ), marker_error
                        raise marker_error

        text_content = (pending.text or "").strip()
        if text_content and text_content != "[empty message]":
            chunks = split_message(text_content, TELEGRAM_MAX_MESSAGE_LEN)
            for idx, chunk in enumerate(chunks):
                try:
                    await self._send_text(chat_id, chunk, reply_params, thread_kwargs)
                    sent_any = True
                except Exception as e:
                    logger.error("Failed to send Telegram text chunk: {}", e)
                    if self._is_retryable_error(e):
                        if sent_any:
                            return pending.with_remaining(
                                text="".join(chunks[idx:]),
                                media=[],
                                last_error=str(e),
                            ), e
                        raise
                    raise

        return None, None

    async def _call_with_retry(self, fn, *args, **kwargs):
        """Call an async Telegram API function with retry on pool/network timeout."""
        for attempt in range(1, _SEND_MAX_RETRIES + 1):
            try:
                return await fn(*args, **kwargs)
            except RetryAfter as exc:
                if attempt == _SEND_MAX_RETRIES:
                    raise
                delay = max(float(getattr(exc, "retry_after", 0) or 0), _SEND_RETRY_BASE_DELAY)
                logger.warning(
                    "Telegram rate limited (attempt {}/{}), retrying in {:.1f}s",
                    attempt,
                    _SEND_MAX_RETRIES,
                    delay,
                )
                await asyncio.sleep(delay)
            except TimedOut:
                if attempt == _SEND_MAX_RETRIES:
                    raise
                delay = _SEND_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    "Telegram timeout (attempt {}/{}), retrying in {:.1f}s",
                    attempt, _SEND_MAX_RETRIES, delay,
                )
                await asyncio.sleep(delay)

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
                "retry queue overflow, dropped oldest message chat_id={}, limit={}",
                dropped_chat_id,
                self.config.send_retry_max_queue,
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
            logger.info("Loaded Telegram retry outbox: {} message(s)", len(loaded))
        except Exception as e:
            logger.error("Failed to load Telegram outbox {}: {}", path, e)

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
                logger.warning("Dropped {} expired Telegram message(s) from retry outbox", dropped)
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
                remainder, remainder_error = await self._deliver_pending(pending)
                if remainder is not None and remainder_error is not None:
                    retryable = self._is_retryable_error(remainder_error)
                    if retryable:
                        self._api_available = False
                    handled = await self._reschedule_or_drop_pending(
                        message_id=pending.message_id,
                        error=remainder_error,
                        retryable=retryable,
                        replacement=remainder,
                    )
                    if handled:
                        logger.warning(
                            "retry_partial_failure message_id={}, chat_id={}, retryable={}, error={}",
                            pending.message_id,
                            pending.chat_id,
                            retryable,
                            remainder_error,
                        )
                    continue
                self._api_available = True
                removed = await self._remove_pending(pending.message_id)
                if removed:
                    logger.info(
                        "retry_success message_id={}, chat_id={}, attempts={}",
                        pending.message_id,
                        pending.chat_id,
                        pending.attempts,
                    )
            except Exception as e:
                retryable = self._is_retryable_error(e)
                if retryable:
                    self._api_available = False
                handled = await self._reschedule_or_drop_pending(
                    message_id=pending.message_id,
                    error=e,
                    retryable=retryable,
                    replacement=None,
                )
                if handled:
                    logger.warning(
                        "retry_failed message_id={}, chat_id={}, retryable={}, error={}",
                        pending.message_id,
                        pending.chat_id,
                        retryable,
                        e,
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

    async def _reschedule_or_drop_pending(
        self,
        message_id: str,
        error: Exception,
        retryable: bool,
        replacement: PendingSend | None = None,
    ) -> bool:
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

            if replacement is not None:
                target.text = replacement.text
                target.media = list(replacement.media)
                target.parse_mode = replacement.parse_mode
                target.reply_to_message_id = replacement.reply_to_message_id
                target.message_thread_id = replacement.message_thread_id

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
                logger.info("expired_dropped_summary_sent chat_id={}, dropped_count={}", chat_id, count)
            except Exception as e:
                if self._is_retryable_error(e):
                    self._api_available = False
                    logger.warning("Failed to send dropped summary (will retry later): {}", e)
                    return
                logger.warning("Failed to send dropped summary (discarded): {}", e)
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
                logger.error("Retry worker error: {}", e)
            await asyncio.sleep(1)

    async def _send_text(
        self,
        chat_id: int,
        text: str,
        reply_params=None,
        thread_kwargs: dict | None = None,
    ) -> None:
        """Send a plain text message with HTML fallback."""
        try:
            html = _markdown_to_telegram_html(text)
            await self._call_with_retry(
                self._app.bot.send_message,
                chat_id=chat_id, text=html, parse_mode="HTML",
                reply_parameters=reply_params,
                **(thread_kwargs or {}),
            )
        except Exception as e:
            logger.warning("HTML parse failed, falling back to plain text: {}", e)
            try:
                await self._call_with_retry(
                    self._app.bot.send_message,
                    chat_id=chat_id,
                    text=text,
                    reply_parameters=reply_params,
                    **(thread_kwargs or {}),
                )
            except Exception as e2:
                logger.error("Error sending Telegram message: {}", e2)
                raise

    @staticmethod
    def _is_not_modified_error(exc: Exception) -> bool:
        return isinstance(exc, BadRequest) and "message is not modified" in str(exc).lower()

    async def send_delta(self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None) -> None:
        """Progressive message editing: send on first delta, edit on subsequent ones."""
        if not self._app:
            return
        meta = metadata or {}
        int_chat_id = int(chat_id)
        stream_id = meta.get("_stream_id")

        if meta.get("_stream_end"):
            buf = self._stream_bufs.get(chat_id)
            if not buf or not buf.message_id or not buf.text:
                return
            if stream_id is not None and buf.stream_id is not None and buf.stream_id != stream_id:
                return
            self._stop_typing(chat_id)
            try:
                html = _markdown_to_telegram_html(buf.text)
                await self._call_with_retry(
                    self._app.bot.edit_message_text,
                    chat_id=int_chat_id, message_id=buf.message_id,
                    text=html, parse_mode="HTML",
                )
            except Exception as e:
                if self._is_not_modified_error(e):
                    logger.debug("Final stream edit already applied for {}", chat_id)
                    self._stream_bufs.pop(chat_id, None)
                    return
                logger.debug("Final stream edit failed (HTML), trying plain: {}", e)
                try:
                    await self._call_with_retry(
                        self._app.bot.edit_message_text,
                        chat_id=int_chat_id, message_id=buf.message_id,
                        text=buf.text,
                    )
                except Exception as e2:
                    if self._is_not_modified_error(e2):
                        logger.debug("Final stream plain edit already applied for {}", chat_id)
                        self._stream_bufs.pop(chat_id, None)
                        return
                    logger.warning("Final stream edit failed: {}", e2)
                    raise  # Let ChannelManager handle retry
            self._stream_bufs.pop(chat_id, None)
            return

        buf = self._stream_bufs.get(chat_id)
        if buf is None or (stream_id is not None and buf.stream_id is not None and buf.stream_id != stream_id):
            buf = _StreamBuf(stream_id=stream_id)
            self._stream_bufs[chat_id] = buf
        elif buf.stream_id is None:
            buf.stream_id = stream_id
        buf.text += delta

        if not buf.text.strip():
            return

        now = time.monotonic()
        if buf.message_id is None:
            try:
                sent = await self._call_with_retry(
                    self._app.bot.send_message,
                    chat_id=int_chat_id, text=buf.text,
                )
                buf.message_id = sent.message_id
                buf.last_edit = now
            except Exception as e:
                logger.warning("Stream initial send failed: {}", e)
                raise  # Let ChannelManager handle retry
        elif (now - buf.last_edit) >= self.config.stream_edit_interval:
            try:
                await self._call_with_retry(
                    self._app.bot.edit_message_text,
                    chat_id=int_chat_id, message_id=buf.message_id,
                    text=buf.text,
                )
                buf.last_edit = now
            except Exception as e:
                if self._is_not_modified_error(e):
                    buf.last_edit = now
                    return
                logger.warning("Stream edit failed: {}", e)
                raise  # Let ChannelManager handle retry

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

    async def _on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help command, bypassing ACL so all users can access it."""
        if not update.message:
            return
        await update.message.reply_text(build_help_text())

    @staticmethod
    def _sender_id(user) -> str:
        """Build sender_id with username for allowlist matching."""
        sid = str(user.id)
        return f"{sid}|{user.username}" if user.username else sid

    @staticmethod
    def _derive_topic_session_key(message) -> str | None:
        """Derive topic-scoped session key for non-private Telegram chats."""
        message_thread_id = getattr(message, "message_thread_id", None)
        if message.chat.type == "private" or message_thread_id is None:
            return None
        return f"telegram:{message.chat_id}:topic:{message_thread_id}"

    @staticmethod
    def _build_message_metadata(message, user) -> dict:
        """Build common Telegram inbound metadata payload."""
        reply_to = getattr(message, "reply_to_message", None)
        return {
            "message_id": message.message_id,
            "user_id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "is_group": message.chat.type != "private",
            "message_thread_id": getattr(message, "message_thread_id", None),
            "is_forum": bool(getattr(message.chat, "is_forum", False)),
            "reply_to_message_id": getattr(reply_to, "message_id", None) if reply_to else None,
        }

    async def _extract_reply_context(self, message) -> str | None:
        """Extract text from the message being replied to, if any."""
        reply = getattr(message, "reply_to_message", None)
        if not reply:
            return None
        text = getattr(reply, "text", None) or getattr(reply, "caption", None) or ""
        if len(text) > TELEGRAM_REPLY_CONTEXT_MAX_LEN:
            text = text[:TELEGRAM_REPLY_CONTEXT_MAX_LEN] + "..."
        if not text:
            return None

        bot_id, _ = await self._ensure_bot_identity()
        reply_user = getattr(reply, "from_user", None)

        if bot_id and reply_user and getattr(reply_user, "id", None) == bot_id:
            return f"[Reply to bot: {text}]"
        if reply_user and getattr(reply_user, "username", None):
            return f"[Reply to @{reply_user.username}: {text}]"
        if reply_user and getattr(reply_user, "first_name", None):
            return f"[Reply to {reply_user.first_name}: {text}]"
        return f"[Reply to: {text}]"

    async def _download_message_media(
        self, msg, *, add_failure_content: bool = False
    ) -> tuple[list[str], list[str]]:
        """Download media from a message (current or reply). Returns (media_paths, content_parts)."""
        media_file = None
        media_type = None
        if getattr(msg, "photo", None):
            media_file = msg.photo[-1]
            media_type = "image"
        elif getattr(msg, "voice", None):
            media_file = msg.voice
            media_type = "voice"
        elif getattr(msg, "audio", None):
            media_file = msg.audio
            media_type = "audio"
        elif getattr(msg, "document", None):
            media_file = msg.document
            media_type = "file"
        elif getattr(msg, "video", None):
            media_file = msg.video
            media_type = "video"
        elif getattr(msg, "video_note", None):
            media_file = msg.video_note
            media_type = "video"
        elif getattr(msg, "animation", None):
            media_file = msg.animation
            media_type = "animation"
        if not media_file or not self._app:
            return [], []
        try:
            file = await self._app.bot.get_file(media_file.file_id)
            ext = self._get_extension(
                media_type,
                getattr(media_file, "mime_type", None),
                getattr(media_file, "file_name", None),
            )
            media_dir = get_media_dir("telegram")
            unique_id = getattr(media_file, "file_unique_id", media_file.file_id)
            file_path = media_dir / f"{unique_id}{ext}"
            await file.download_to_drive(str(file_path))
            path_str = str(file_path)
            if media_type in ("voice", "audio"):
                transcription = await self.transcribe_audio(file_path)
                if transcription:
                    logger.info("Transcribed {}: {}...", media_type, transcription[:50])
                    return [path_str], [f"[transcription: {transcription}]"]
                return [path_str], [f"[{media_type}: {path_str}]"]
            return [path_str], [f"[{media_type}: {path_str}]"]
        except Exception as e:
            logger.warning("Failed to download message media: {}", e)
            if add_failure_content:
                return [], [f"[{media_type}: download failed]"]
            return [], []

    async def _ensure_bot_identity(self) -> tuple[int | None, str | None]:
        """Load bot identity once and reuse it for mention/reply checks."""
        if self._bot_user_id is not None or self._bot_username is not None:
            return self._bot_user_id, self._bot_username
        if not self._app:
            return None, None
        bot_info = await self._app.bot.get_me()
        self._bot_user_id = getattr(bot_info, "id", None)
        self._bot_username = getattr(bot_info, "username", None)
        return self._bot_user_id, self._bot_username

    @staticmethod
    def _has_mention_entity(
        text: str,
        entities,
        bot_username: str,
        bot_id: int | None,
    ) -> bool:
        """Check Telegram mention entities against the bot username."""
        handle = f"@{bot_username}".lower()
        for entity in entities or []:
            entity_type = getattr(entity, "type", None)
            if entity_type == "text_mention":
                user = getattr(entity, "user", None)
                if user is not None and bot_id is not None and getattr(user, "id", None) == bot_id:
                    return True
                continue
            if entity_type != "mention":
                continue
            offset = getattr(entity, "offset", None)
            length = getattr(entity, "length", None)
            if offset is None or length is None:
                continue
            if text[offset : offset + length].lower() == handle:
                return True
        return handle in text.lower()

    async def _is_group_message_for_bot(self, message) -> bool:
        """Allow group messages when policy is open, @mentioned, or replying to the bot."""
        if message.chat.type == "private" or self.config.group_policy == "open":
            return True

        bot_id, bot_username = await self._ensure_bot_identity()
        if bot_username:
            text = message.text or ""
            caption = message.caption or ""
            if self._has_mention_entity(
                text,
                getattr(message, "entities", None),
                bot_username,
                bot_id,
            ):
                return True
            if self._has_mention_entity(
                caption,
                getattr(message, "caption_entities", None),
                bot_username,
                bot_id,
            ):
                return True

        reply_user = getattr(getattr(message, "reply_to_message", None), "from_user", None)
        return bool(bot_id and reply_user and reply_user.id == bot_id)

    def _remember_thread_context(self, message) -> None:
        """Cache topic thread id by chat/message id for follow-up replies."""
        message_thread_id = getattr(message, "message_thread_id", None)
        if message_thread_id is None:
            return
        key = (str(message.chat_id), message.message_id)
        self._message_threads[key] = message_thread_id
        if len(self._message_threads) > 1000:
            self._message_threads.pop(next(iter(self._message_threads)))

    async def _forward_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Forward slash commands to the bus for unified handling in AgentLoop."""
        if not update.message or not update.effective_user:
            return
        message = update.message
        user = update.effective_user
        self._remember_thread_context(message)
        await self._handle_message(
            sender_id=self._sender_id(user),
            chat_id=str(message.chat_id),
            content=self._normalize_telegram_command(message.text or ""),
            metadata=self._build_message_metadata(message, user),
            session_key=self._derive_topic_session_key(message),
        )

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming messages (text, photos, voice, documents)."""
        if not update.message or not update.effective_user:
            return

        message = update.message
        user = update.effective_user
        chat_id = message.chat_id
        sender_id = self._sender_id(user)
        self._remember_thread_context(message)

        # Store chat_id for replies
        self._chat_ids[sender_id] = chat_id

        if not await self._is_group_message_for_bot(message):
            return

        # Build content from text and/or media
        content_parts = []
        media_paths = []

        # Text content
        if message.text:
            content_parts.append(message.text)
        if message.caption:
            content_parts.append(message.caption)

        # Location content
        if message.location:
            lat = message.location.latitude
            lon = message.location.longitude
            content_parts.append(f"[location: {lat}, {lon}]")

        # Download current message media
        current_media_paths, current_media_parts = await self._download_message_media(
            message, add_failure_content=True
        )
        media_paths.extend(current_media_paths)
        content_parts.extend(current_media_parts)
        if current_media_paths:
            logger.debug("Downloaded message media to {}", current_media_paths[0])

        # Reply context: text and/or media from the replied-to message
        reply = getattr(message, "reply_to_message", None)
        if reply is not None:
            reply_ctx = await self._extract_reply_context(message)
            reply_media, reply_media_parts = await self._download_message_media(reply)
            if reply_media:
                media_paths = reply_media + media_paths
                logger.debug("Attached replied-to media: {}", reply_media[0])
            tag = reply_ctx or (f"[Reply to: {reply_media_parts[0]}]" if reply_media_parts else None)
            if tag:
                content_parts.insert(0, tag)
        content = "\n".join(content_parts) if content_parts else "[empty message]"

        logger.debug("Telegram message from {}: {}...", sender_id, content[:50])

        str_chat_id = str(chat_id)
        metadata = self._build_message_metadata(message, user)
        session_key = self._derive_topic_session_key(message)

        # Telegram media groups: buffer briefly, forward as one aggregated turn.
        if media_group_id := getattr(message, "media_group_id", None):
            key = f"{str_chat_id}:{media_group_id}"
            if key not in self._media_group_buffers:
                self._media_group_buffers[key] = {
                    "sender_id": sender_id, "chat_id": str_chat_id,
                    "contents": [], "media": [],
                    "metadata": metadata,
                    "session_key": session_key,
                }
                self._start_typing(str_chat_id)
                await self._add_reaction(str_chat_id, message.message_id, self.config.react_emoji)
            buf = self._media_group_buffers[key]
            if content and content != "[empty message]":
                buf["contents"].append(content)
            buf["media"].extend(media_paths)
            if key not in self._media_group_tasks:
                self._media_group_tasks[key] = asyncio.create_task(self._flush_media_group(key))
            return

        # Start typing indicator before processing
        self._start_typing(str_chat_id)
        await self._add_reaction(str_chat_id, message.message_id, self.config.react_emoji)

        # Forward to the message bus
        await self._handle_message(
            sender_id=sender_id,
            chat_id=str_chat_id,
            content=content,
            media=media_paths,
            metadata=metadata,
            session_key=session_key,
        )

    async def _flush_media_group(self, key: str) -> None:
        """Wait briefly, then forward buffered media-group as one turn."""
        try:
            await asyncio.sleep(0.6)
            if not (buf := self._media_group_buffers.pop(key, None)):
                return
            content = "\n".join(buf["contents"]) or "[empty message]"
            await self._handle_message(
                sender_id=buf["sender_id"], chat_id=buf["chat_id"],
                content=content, media=list(dict.fromkeys(buf["media"])),
                metadata=buf["metadata"],
                session_key=buf.get("session_key"),
            )
        finally:
            self._media_group_tasks.pop(key, None)

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

    async def _add_reaction(self, chat_id: str, message_id: int, emoji: str) -> None:
        """Add emoji reaction to a message (best-effort, non-blocking)."""
        if not self._app or not emoji:
            return
        try:
            await self._app.bot.set_message_reaction(
                chat_id=int(chat_id),
                message_id=message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
            )
        except Exception as e:
            logger.debug("Telegram reaction failed: {}", e)

    async def _typing_loop(self, chat_id: str) -> None:
        """Repeatedly send 'typing' action until cancelled."""
        try:
            while self._app:
                await self._app.bot.send_chat_action(chat_id=int(chat_id), action="typing")
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("Typing indicator stopped for {}: {}", chat_id, e)

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log polling / handler errors instead of silently swallowing them."""
        from telegram.error import NetworkError, TimedOut
        
        if isinstance(context.error, (NetworkError, TimedOut)):
            logger.warning("Telegram network issue: {}", str(context.error))
        else:
            logger.error("Telegram error: {}", context.error)

    def _get_extension(
        self,
        media_type: str,
        mime_type: str | None,
        filename: str | None = None,
    ) -> str:
        """Get file extension based on media type or original filename."""
        if mime_type:
            ext_map = {
                "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
            }
            if mime_type in ext_map:
                return ext_map[mime_type]

        type_map = {"image": ".jpg", "voice": ".ogg", "audio": ".mp3", "file": ""}
        if ext := type_map.get(media_type, ""):
            return ext

        if filename:
            from pathlib import Path

            return "".join(Path(filename).suffixes)

        return ""
