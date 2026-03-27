"""Shared helpers for Weixin allowFrom broadcasts."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".nanobot" / "config.json"
DEFAULT_TIMEOUT = 30


@dataclass(frozen=True)
class WeixinBroadcastTargets:
    """Resolved bridge target set for allowFrom broadcasts."""

    bridge_url: str = ""
    bridge_token: str = ""
    recipients: tuple[str, ...] = ()


def strip_basic_markdown(text: str) -> str:
    """Strip the small markdown subset our bridge recipients should not see."""
    return text.replace("*", "").replace("_", "").replace("`", "")


def resolve_weixin_allowfrom_targets(config_path: Path = DEFAULT_CONFIG_PATH) -> WeixinBroadcastTargets:
    """Resolve current Weixin bridge recipients from config and saved account state."""
    if not config_path.exists():
        return WeixinBroadcastTargets()

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return WeixinBroadcastTargets()

    weixin_cfg = raw.get("channels", {}).get("weixin", {})
    if not weixin_cfg.get("enabled"):
        return WeixinBroadcastTargets()

    bridge_url = str(weixin_cfg.get("bridgeUrl") or "").strip()
    bridge_token = str(weixin_cfg.get("bridgeToken") or "").strip()
    allow_from = [
        str(item).strip()
        for item in weixin_cfg.get("allowFrom", [])
        if str(item).strip() and str(item).strip() != "*"
    ]
    state_dir = Path(str(weixin_cfg.get("stateDir") or (Path.home() / ".nanobot" / "weixin-auth")))
    accounts_dir = state_dir / "accounts"

    recipients: list[str] = []
    if accounts_dir.exists():
        for account_file in sorted(accounts_dir.glob("*.json")):
            try:
                account = json.loads(account_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            account_id = str(account.get("accountId") or "").strip()
            if not account_id:
                continue
            for user_id in allow_from:
                recipients.append(f"{account_id}|{user_id}")

    deduped = tuple(dict.fromkeys(recipients))
    return WeixinBroadcastTargets(
        bridge_url=bridge_url,
        bridge_token=bridge_token,
        recipients=deduped,
    )


async def send_weixin_broadcast_async(
    text: str,
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    timeout: int = DEFAULT_TIMEOUT,
    targets: WeixinBroadcastTargets | None = None,
) -> dict[str, str]:
    """Broadcast one text payload to all current allowFrom targets."""
    targets = targets or resolve_weixin_allowfrom_targets(config_path)
    if not targets.bridge_url or not targets.recipients:
        return {}

    import websockets

    plain_text = strip_basic_markdown(text)
    results: dict[str, str] = {}

    async with websockets.connect(
        targets.bridge_url,
        open_timeout=timeout,
        close_timeout=timeout,
    ) as ws:
        if targets.bridge_token:
            await ws.send(json.dumps({"type": "auth", "token": targets.bridge_token}, ensure_ascii=False))

        for recipient in targets.recipients:
            await ws.send(
                json.dumps(
                    {
                        "type": "send",
                        "to": recipient,
                        "text": plain_text,
                        "media": [],
                    },
                    ensure_ascii=False,
                )
            )
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                message = json.loads(raw)
                msg_type = message.get("type")
                if msg_type == "heartbeat":
                    continue
                if msg_type == "sent" and message.get("to") == recipient:
                    results[recipient] = "sent"
                    break
                if msg_type == "error":
                    results[recipient] = f"error: {message.get('error', 'unknown')}"
                    break

    return results


def send_weixin_broadcast(
    text: str,
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, str]:
    """Synchronous wrapper for allowFrom broadcasts."""
    return asyncio.run(
        send_weixin_broadcast_async(
            text,
            config_path=config_path,
            timeout=timeout,
        )
    )
