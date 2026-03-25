"""Weixin channel implementation using an official-compatible Node.js bridge."""

import asyncio
import json
from collections import OrderedDict
from time import monotonic
from typing import Any

from loguru import logger
from pydantic import Field

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Base


class WeixinConfig(Base):
    """Weixin channel configuration."""

    enabled: bool = False
    bridge_url: str = "ws://127.0.0.1:3002"
    bridge_token: str = ""
    base_url: str = "https://ilinkai.weixin.qq.com"
    state_dir: str = ""
    accounts: list[str] = Field(default_factory=list)
    typing_enabled: bool = True
    media_enabled: bool = True
    allow_from: list[str] = Field(default_factory=list)


class WeixinChannel(BaseChannel):
    """Weixin channel backed by a local bridge process."""

    _RECONNECT_DELAY_SECONDS = 5
    _SEND_RETRY_WAIT_SECONDS = 3
    _HEARTBEAT_INTERVAL_SECONDS = 10
    _HEARTBEAT_TIMEOUT_SECONDS = 45

    name = "weixin"
    display_name = "Weixin"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return WeixinConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = WeixinConfig.model_validate(config)
        super().__init__(config, bus)
        self._ws = None
        self._connected = False
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()
        self._connected_event = asyncio.Event()
        self._last_heartbeat_at = 0.0
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._account_status: dict[str, str] = {}

    async def start(self) -> None:
        """Start the Weixin channel by connecting to the bridge."""
        import websockets

        logger.info("Connecting to Weixin bridge at {}...", self.config.bridge_url)
        self._running = True

        while self._running:
            try:
                async with websockets.connect(self.config.bridge_url) as ws:
                    self._ws = ws
                    if self.config.bridge_token:
                        await ws.send(json.dumps({"type": "auth", "token": self.config.bridge_token}))
                    self._mark_transport_connected()
                    logger.info("Connected to Weixin bridge")
                    self._heartbeat_task = asyncio.create_task(self._watch_bridge_heartbeat(ws))

                    async for message in ws:
                        try:
                            await self._handle_bridge_message(message)
                        except Exception as e:
                            logger.error("Error handling Weixin bridge message: {}", e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Weixin bridge connection error: {}", e)
            finally:
                await self._set_transport_disconnected("bridge_stream_closed")
            if self._running:
                logger.info("Reconnecting to Weixin bridge in {} seconds...", self._RECONNECT_DELAY_SECONDS)
                await asyncio.sleep(self._RECONNECT_DELAY_SECONDS)

    async def stop(self) -> None:
        """Stop the Weixin channel."""
        self._running = False
        await self._set_transport_disconnected("channel_stop", close_ws=True)

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Weixin."""
        payload = {
            "type": "send",
            "to": msg.chat_id,
            "text": msg.content,
        }
        for attempt in range(2):
            if not await self._wait_for_connection():
                logger.warning(
                    "Weixin bridge not connected chat_id={} attempt={}",
                    msg.chat_id,
                    attempt + 1,
                )
                continue
            ws = self._ws
            if not ws:
                continue
            try:
                await ws.send(json.dumps(payload, ensure_ascii=False))
                return
            except Exception as e:
                logger.error("Error sending Weixin message chat_id={} attempt={} error={}", msg.chat_id, attempt + 1, e)
                await self._set_transport_disconnected("send_failed", close_ws=True)
        logger.warning("Weixin send dropped after reconnect attempts chat_id={}", msg.chat_id)

    async def _handle_bridge_message(self, raw: str) -> None:
        """Handle a message from the bridge."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON from Weixin bridge: {}", raw[:100])
            return

        msg_type = data.get("type")
        if msg_type == "message":
            account_id = str(data.get("accountId", "")).strip()
            sender = str(data.get("sender", "")).strip()
            content = str(data.get("content", "")).strip()
            media = [
                str(item).strip()
                for item in (data.get("media") or [])
                if str(item).strip()
            ]
            if not self.config.media_enabled:
                media = []
            message_id = str(data.get("id", "")).strip()
            composite_id = f"{account_id}:{message_id}" if account_id and message_id else message_id

            if composite_id:
                if composite_id in self._processed_message_ids:
                    return
                self._processed_message_ids[composite_id] = None
                while len(self._processed_message_ids) > 1000:
                    self._processed_message_ids.popitem(last=False)

            if not account_id or not sender:
                logger.warning("Weixin bridge message missing accountId or sender")
                return

            if media and not content:
                content = "\n".join(f"[image: {path.rsplit('/', 1)[-1]}]" for path in media)
            if media:
                logger.info(
                    "Weixin inbound media forwarded account={} sender={} count={}",
                    account_id,
                    sender,
                    len(media),
                )

            await self._handle_message(
                sender_id=sender,
                chat_id=f"{account_id}|{sender}",
                content=content,
                media=media,
                metadata={
                    "message_id": message_id,
                },
            )
        elif msg_type == "status":
            status = data.get("status")
            account_id = str(data.get("accountId") or "").strip()
            if account_id:
                self._account_status[account_id] = str(status or "")
            logger.info(
                "Weixin status: {} account={} detail={}",
                status,
                account_id,
                data.get("detail"),
            )
        elif msg_type == "heartbeat":
            self._last_heartbeat_at = monotonic()
        elif msg_type == "qr":
            logger.info("Scan the QR code shown in the bridge terminal to connect Weixin")
        elif msg_type == "error":
            logger.error("Weixin bridge error: {}", data.get("error"))

    def _mark_transport_connected(self) -> None:
        self._connected = True
        self._connected_event.set()
        self._last_heartbeat_at = monotonic()

    async def _set_transport_disconnected(self, reason: str, *, close_ws: bool = False) -> None:
        ws = self._ws
        self._ws = None
        self._connected = False
        self._connected_event.clear()
        self._last_heartbeat_at = 0.0
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        if close_ws and ws:
            try:
                await ws.close()
            except Exception:
                logger.debug("Ignoring Weixin websocket close failure during {}", reason)
        logger.info("Weixin transport disconnected reason={}", reason)

    async def _wait_for_connection(self) -> bool:
        if self._connected and self._ws:
            return True
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=self._SEND_RETRY_WAIT_SECONDS)
        except asyncio.TimeoutError:
            return False
        return bool(self._connected and self._ws)

    async def _watch_bridge_heartbeat(self, ws: Any) -> None:
        try:
            while self._running and self._ws is ws:
                await asyncio.sleep(self._HEARTBEAT_INTERVAL_SECONDS)
                if not self._connected or self._ws is not ws:
                    return
                if monotonic() - self._last_heartbeat_at <= self._HEARTBEAT_TIMEOUT_SECONDS:
                    continue
                logger.warning(
                    "Weixin bridge heartbeat timed out after {} seconds",
                    self._HEARTBEAT_TIMEOUT_SECONDS,
                )
                await self._set_transport_disconnected("heartbeat_timeout", close_ws=True)
                return
        except asyncio.CancelledError:
            return
