#!/usr/bin/env python3
"""Migrate legacy Weixin bridge state to direct-channel account.json."""

from __future__ import annotations

import argparse
from pathlib import Path

from nanobot.utils.weixin_state_migration import (
    default_direct_weixin_state_dir,
    default_legacy_weixin_state_dir,
    migrate_legacy_weixin_state,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--legacy-state-dir",
        default=str(default_legacy_weixin_state_dir()),
        help="Path to legacy ~/.nanobot/weixin-auth directory",
    )
    parser.add_argument(
        "--state-dir",
        default=str(default_direct_weixin_state_dir()),
        help="Path to direct-channel ~/.nanobot/weixin directory",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing direct-channel account.json",
    )
    args = parser.parse_args()

    result = migrate_legacy_weixin_state(
        legacy_state_dir=Path(args.legacy_state_dir).expanduser(),
        state_dir=Path(args.state_dir).expanduser(),
        force=args.force,
    )
    print(f"Migrated legacy Weixin account {result.account_id} -> {result.state_path}")


if __name__ == "__main__":
    main()
