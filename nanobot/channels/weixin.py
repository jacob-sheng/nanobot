"""Weixin channel implementation using an official-compatible Node.js bridge."""

import asyncio
import json
from collections import OrderedDict
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
                    self._connected = True
                    logger.info("Connected to Weixin bridge")

                    async for message in ws:
                        try:
                            await self._handle_bridge_message(message)
                        except Exception as e:
                            logger.error("Error handling Weixin bridge message: {}", e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                self._ws = None
                logger.warning("Weixin bridge connection error: {}", e)
                if self._running:
                    logger.info("Reconnecting to Weixin bridge in 5 seconds...")
                    await asyncio.sleep(5)

    async def stop(self) -> None:
        """Stop the Weixin channel."""
        self._running = False
        self._connected = False
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Weixin."""
        if not self._ws or not self._connected:
            logger.warning("Weixin bridge not connected")
            return

        try:
            payload = {
                "type": "send",
                "to": msg.chat_id,
                "text": msg.content,
                "metadata": msg.metadata,
            }
            await self._ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            logger.error("Error sending Weixin message: {}", e)

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
                    "timestamp": data.get("timestamp"),
                    "account_id": account_id,
                    "context_token": data.get("contextToken"),
                },
            )
        elif msg_type == "status":
            status = data.get("status")
            if status == "connected":
                self._connected = True
            elif status == "disconnected":
                self._connected = False
            logger.info(
                "Weixin status: {} account={} detail={}",
                status,
                data.get("accountId"),
                data.get("detail"),
            )
        elif msg_type == "qr":
            logger.info("Scan the QR code shown in the bridge terminal to connect Weixin")
        elif msg_type == "error":
            logger.error("Weixin bridge error: {}", data.get("error"))
