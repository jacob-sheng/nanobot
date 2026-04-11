"""Utilities for migrating legacy Weixin bridge state to direct-channel state."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def default_legacy_weixin_state_dir(home: Path | None = None) -> Path:
    root = home or Path.home()
    return root / ".nanobot" / "weixin-auth"


def default_direct_weixin_state_dir(home: Path | None = None) -> Path:
    root = home or Path.home()
    return root / ".nanobot" / "weixin"


def _normalize_context_tokens(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(user_id).strip(): str(token).strip()
        for user_id, token in value.items()
        if str(user_id).strip() and str(token).strip()
    }


@dataclass(frozen=True)
class WeixinStateMigrationResult:
    account_id: str
    state_path: Path
    legacy_account_path: Path
    legacy_sync_path: Path | None


def migrate_legacy_weixin_state(
    legacy_state_dir: Path,
    state_dir: Path,
    *,
    force: bool = False,
) -> WeixinStateMigrationResult:
    """Convert one legacy ``weixin-auth`` account into direct-channel ``account.json``."""
    legacy_state_dir = legacy_state_dir.expanduser().resolve()
    state_dir = state_dir.expanduser().resolve()

    accounts_dir = legacy_state_dir / "accounts"
    if not accounts_dir.exists():
        raise FileNotFoundError(f"Legacy accounts directory not found: {accounts_dir}")

    account_files = sorted(accounts_dir.glob("*.json"))
    if not account_files:
        raise RuntimeError(f"No legacy Weixin account files found in {accounts_dir}")
    if len(account_files) != 1:
        raise RuntimeError(
            f"Expected exactly one legacy Weixin account, found {len(account_files)} in {accounts_dir}"
        )

    account_file = account_files[0]
    account_data = json.loads(account_file.read_text(encoding="utf-8"))
    account_id = str(account_data.get("accountId") or account_file.stem).strip()
    token = str(account_data.get("token") or "").strip()
    base_url = str(account_data.get("baseUrl") or account_data.get("base_url") or "").strip()
    if not token:
        raise RuntimeError(f"Legacy Weixin account missing token: {account_file}")
    if not base_url:
        raise RuntimeError(f"Legacy Weixin account missing baseUrl: {account_file}")

    sync_file = legacy_state_dir / "sync" / f"{account_id}.json"
    get_updates_buf = ""
    if sync_file.exists():
        sync_data = json.loads(sync_file.read_text(encoding="utf-8"))
        get_updates_buf = str(
            sync_data.get("getUpdatesBuf")
            or sync_data.get("get_updates_buf")
            or ""
        ).strip()

    state_path = state_dir / "account.json"
    if state_path.exists() and not force:
        raise FileExistsError(f"Target direct Weixin state already exists: {state_path}")

    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "token": token,
        "get_updates_buf": get_updates_buf,
        "context_tokens": _normalize_context_tokens(
            account_data.get("contextTokens") or account_data.get("context_tokens")
        ),
        "typing_tickets": {},
        "base_url": base_url,
    }
    state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return WeixinStateMigrationResult(
        account_id=account_id,
        state_path=state_path,
        legacy_account_path=account_file,
        legacy_sync_path=sync_file if sync_file.exists() else None,
    )
