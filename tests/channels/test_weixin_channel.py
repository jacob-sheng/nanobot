import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.channels.weixin import WeixinChannel, WeixinConfig


def _make_channel() -> tuple[WeixinChannel, MessageBus]:
    bus = MessageBus()
    channel = WeixinChannel(
        WeixinConfig(enabled=True, allow_from=["*"]),
        bus,
    )
    return channel, bus


@pytest.mark.asyncio
async def test_bridge_message_forwards_text_and_media() -> None:
    channel, bus = _make_channel()

    raw = json.dumps(
        {
            "type": "message",
            "accountId": "acct-1",
            "sender": "wx-user",
            "id": "m1",
            "content": "",
            "media": ["/tmp/test.jpg"],
            "contextToken": "ctx-1",
        },
        ensure_ascii=False,
    )

    await channel._handle_bridge_message(raw)
    inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=1.0)

    assert inbound.sender_id == "wx-user"
    assert inbound.chat_id == "acct-1|wx-user"
    assert "[image: test.jpg]" in inbound.content
    assert inbound.media == ["/tmp/test.jpg"]
    assert inbound.metadata["context_token"] == "ctx-1"


@pytest.mark.asyncio
async def test_bridge_message_deduplicates_account_scoped_message_ids() -> None:
    channel, bus = _make_channel()
    raw = json.dumps(
        {
            "type": "message",
            "accountId": "acct-1",
            "sender": "wx-user",
            "id": "m2",
            "content": "hello",
        }
    )

    await channel._handle_bridge_message(raw)
    first = await asyncio.wait_for(bus.consume_inbound(), timeout=1.0)
    await channel._handle_bridge_message(raw)

    assert first.content == "hello"
    assert bus.inbound_size == 0


@pytest.mark.asyncio
async def test_status_message_updates_connection_flag() -> None:
    channel, _bus = _make_channel()
    channel._connected = False

    await channel._handle_bridge_message(json.dumps({"type": "status", "status": "connected"}))
    assert channel._connected is True

    await channel._handle_bridge_message(json.dumps({"type": "status", "status": "disconnected"}))
    assert channel._connected is False


@pytest.mark.asyncio
async def test_send_uses_websocket_when_connected() -> None:
    channel, _bus = _make_channel()
    ws = AsyncMock()
    channel._ws = ws
    channel._connected = True

    msg = type(
        "Msg",
        (),
        {"chat_id": "acct-1|wx-user", "content": "pong", "media": [], "metadata": {"x": 1}},
    )()
    await channel.send(msg)

    ws.send.assert_awaited_once()
    sent_payload = json.loads(ws.send.await_args.args[0])
    assert sent_payload["type"] == "send"
    assert sent_payload["to"] == "acct-1|wx-user"
    assert sent_payload["text"] == "pong"
    assert sent_payload["metadata"] == {"x": 1}
