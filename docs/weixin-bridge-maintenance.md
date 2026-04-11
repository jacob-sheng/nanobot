# Weixin Direct-Channel Maintenance

This environment now follows upstream's direct HTTP long-poll Weixin channel.
There is no standalone Weixin bridge service anymore.

## Current Architecture

- Runtime channel:
  - `nanobot/channels/weixin.py`
- Weixin mirror helper:
  - `nanobot/utils/weixin_broadcast.py`
- Legacy state migration:
  - `nanobot/utils/weixin_state_migration.py`
  - `scripts/migrate_weixin_state.py`
- Persistent direct-channel state:
  - `~/.nanobot/weixin/account.json`
- Legacy migration source kept for rollback only:
  - `~/.nanobot/weixin-auth/`

The direct channel owns:

- QR login
- long-poll receive loop
- `context_tokens` persistence
- typing ticket cache
- inbound media download and decrypt
- outbound media upload and send

Conversation text history is **not** stored in Weixin state; it lives under
`~/.nanobot/workspace/sessions/`.

## State Files

- `~/.nanobot/weixin/account.json`
  - stores `token`, `get_updates_buf`, persisted `context_tokens`, `typing_tickets`, and `base_url`
- `~/.nanobot/weixin-auth/`
  - legacy bridge-era backup material used only by the migration script

## Local Guarantees To Preserve

- keep the upstream direct-channel architecture; do not reintroduce a standalone Weixin bridge
- do not expose transport-only metadata like `context_token` to prompt construction
- keep the shared "inject current time only after 10 minutes of idle" policy for chat channels, with Weixin participating in the same idle-gap hint behavior
- keep direct state under `~/.nanobot/weixin/account.json`
- keep `mirrorWeixinAllowFrom` working for daily digest and curated cron shares via `channels.weixin.allowFrom`
- keep inbound image parsing aligned with Tencent's `@tencent-weixin/openclaw-weixin` semantics:
  - parse `item_list`
  - match typed media items
  - prefer `image_item.aeskey` for images and `media.aes_key` elsewhere
  - use CDN `encrypt_query_param` / `full_url`

## Troubleshooting

- Login or polling fails:
  - inspect `journalctl -u nanobot-gateway.service`
  - look for `WeChat login failed`, `getUpdates failed`, or session-expired warnings
- Replies are skipped:
  - inspect `~/.nanobot/weixin/account.json`
  - confirm `context_tokens` contains the target `allowFrom` user
  - if the recipient is missing a token, direct mirrors intentionally report `missing_context_token`
- Inbound images do not reach the model:
  - inspect `journalctl -u nanobot-gateway.service`
  - look for `WeChat media download failed` or `WeChat inbound`
  - if `~/.nanobot/media/weixin/` stays empty, the direct channel has not completed download/decrypt yet
- Legacy state must be converted:
  - run `scripts/migrate_weixin_state.py`
  - verify `~/.nanobot/weixin/account.json` exists before restarting the gateway

## Upgrade Checklist

1. Review upstream Weixin commits in `nanobot/channels/weixin.py`.
2. Re-check local helpers:
   - `nanobot/utils/weixin_broadcast.py`
   - `nanobot/utils/weixin_state_migration.py`
3. Run focused Weixin tests.
4. Restart `nanobot-gateway.service`.
5. Verify:
   - text send/receive
   - inbound image forwarding
   - outbound media send
   - QR login still works via `nanobot channels login weixin`
   - daily digest Weixin mirror still works via `channels.weixin.allowFrom`
