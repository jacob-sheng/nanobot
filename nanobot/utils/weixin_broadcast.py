"""Shared helpers for Weixin allowFrom broadcasts."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from nanobot.bus.queue import MessageBus
from nanobot.channels.weixin import WeixinChannel, WeixinConfig

DEFAULT_CONFIG_PATH = Path.home() / ".nanobot" / "config.json"
DEFAULT_TIMEOUT = 30


@dataclass
class WeixinBroadcastTargets:
    """Resolved direct-channel target set for allowFrom broadcasts."""

    base_url: str = ""
    token: str = ""
    route_tag: str | int | None = None
    state_path: Path | None = None
    recipients: tuple[str, ...] = ()
    context_tokens: dict[str, str] = field(default_factory=dict)


def strip_basic_markdown(text: str) -> str:
    """Strip the small markdown subset our direct-channel recipients should not see."""
    return text.replace("*", "").replace("_", "").replace("`", "")


def _normalize_context_tokens(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(user_id).strip(): str(token).strip()
        for user_id, token in value.items()
        if str(user_id).strip() and str(token).strip()
    }


def resolve_weixin_allowfrom_targets(config_path: Path = DEFAULT_CONFIG_PATH) -> WeixinBroadcastTargets:
    """Resolve current Weixin direct-channel recipients from config and saved state."""
    if not config_path.exists():
        return WeixinBroadcastTargets()

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return WeixinBroadcastTargets()

    weixin_cfg = raw.get("channels", {}).get("weixin", {})
    if not weixin_cfg.get("enabled"):
        return WeixinBroadcastTargets()

    allow_from = [
        str(item).strip()
        for item in weixin_cfg.get("allowFrom", [])
        if str(item).strip() and str(item).strip() != "*"
    ]
    state_dir = Path(str(weixin_cfg.get("stateDir") or (Path.home() / ".nanobot" / "weixin")))
    state_path = state_dir / "account.json"

    state: dict[str, Any] = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    recipients = tuple(dict.fromkeys(allow_from))
    return WeixinBroadcastTargets(
        base_url=str(weixin_cfg.get("baseUrl") or state.get("base_url") or "https://ilinkai.weixin.qq.com").strip(),
        token=str(weixin_cfg.get("token") or state.get("token") or "").strip(),
        route_tag=weixin_cfg.get("routeTag"),
        state_path=state_path,
        recipients=recipients,
        context_tokens=_normalize_context_tokens(state.get("context_tokens")),
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
    if not targets.base_url or not targets.token or not targets.recipients:
        return {}

    plain_text = strip_basic_markdown(text)
    results: dict[str, str] = {}
    channel = WeixinChannel(
        WeixinConfig(
            enabled=True,
            allow_from=["*"],
            base_url=targets.base_url,
            route_tag=targets.route_tag,
            state_dir=str(targets.state_path.parent) if targets.state_path else "",
        ),
        MessageBus(),
    )
    channel._token = targets.token
    channel._context_tokens = dict(targets.context_tokens)
    channel._client = httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=min(timeout, 30)),
        follow_redirects=True,
    )
    try:
        for recipient in targets.recipients:
            context_token = targets.context_tokens.get(recipient, "").strip()
            if not context_token:
                results[recipient] = "missing_context_token"
                continue
            try:
                await channel._send_text(recipient, plain_text, context_token)
            except Exception as exc:
                results[recipient] = f"error: {exc}"
            else:
                results[recipient] = "sent"
    finally:
        await channel._client.aclose()
        channel._client = None

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
