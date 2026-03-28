# Weixin Bridge Maintenance

This environment intentionally keeps a local Node-based Weixin bridge instead of
switching back to upstream's direct HTTP long-poll channel.

## Current Architecture

- Python runtime channel:
  - `nanobot/channels/weixin.py`
- Node bridge runtime:
  - `bridge/src/weixin-api.ts`
  - `bridge/src/weixin-auth.ts`
  - `bridge/src/weixin-index.ts`
  - `bridge/src/weixin.ts`
- Persistent auth / bridge state:
  - `~/.nanobot/weixin-auth/accounts/*.json`
  - `~/.nanobot/weixin-auth/sync/*.json`
- Long-running host:
  - `nanobot-weixin-bridge.service`

The bridge owns:

- QR login
- polling and error classification
- `contextTokens` routing state
- inbound media download
- outbound media upload / send

The Python channel owns:

- websocket transport to the bridge
- `InboundMessage` creation
- prompt-safe metadata filtering
- heartbeat timeout handling

## State Files

- `accounts/*.json`
  - stores account token, user/account IDs, base URL, and persisted `contextTokens`
- `sync/*.json`
  - stores `getUpdatesBuf`

Conversation text history is **not** stored in `weixin-auth`; it lives under
`~/.nanobot/workspace/sessions/`.

## Upstream Weixin Fixes Worth Re-checking

When upstream `HKUDS/nanobot` changes Weixin support, re-check these areas first:

- `3a9d6ea` route tag / `SKRouteTag` compatibility
- `1f5492e` persisted context tokens
- `9c872c3` session expired / invalid session retry behavior
- `48902ae` QR auto-refresh
- `11e1bbb` outbound media send via CDN upload
- `0dad612` / `0ccfcf6` version migration and compatibility updates

## Local Guarantees To Preserve

- keep the bridge architecture; do not swap in upstream direct Weixin without a dedicated migration
- do not expose transport-only metadata like `context_token` to prompt construction
- keep the "inject current time only after 10 minutes of idle" policy for Weixin sessions
- keep QR refresh writing `~/weixin-qr.png`
- keep bridge restart behavior compatible with `~/.nanobot/weixin-auth`
- keep inbound image parsing aligned with Tencent's `@tencent-weixin/openclaw-weixin` plugin:
  - parse `item_list`
  - match `type=IMAGE`
  - read `image_item.media.encrypt_query_param`
  - decrypt CDN bytes with `image_item.aeskey` or `image_item.media.aes_key`
  - do not treat "image URL scraping" as the primary path

## Troubleshooting

- Bridge connected but replies fail:
  - inspect `contextTokens` in `accounts/*.json`
  - inspect `journalctl -u nanobot-weixin-bridge.service`
  - look for `weixin.send_invalid_context_token` or `weixin.send_session_expired`
- Polling noise / silence:
  - inspect `sync/*.json`
  - inspect `weixin.monitor_session_expired`, `weixin.monitor_api_error`, `weixin.monitor_network_error`
- Inbound images not reaching the model:
  - inspect `journalctl -u nanobot-weixin-bridge.service`
  - look for `weixin.inbound_item_summary`, `weixin.image_download_start`, `weixin.image_saved`, `weixin.image_download_failed`
  - if `~/.nanobot/media/weixin/` stays empty, the bridge has not completed CDN download/decrypt yet
  - compare the current `item_list` structure against Tencent's official `@tencent-weixin/openclaw-weixin` plugin before changing parsing logic
- Gateway says bridge disconnected:
  - inspect `journalctl -u nanobot-gateway.service`
  - inspect heartbeat events and websocket close reasons

## Upgrade Checklist

1. Review upstream Weixin commits against the files listed above.
2. Port only behavior that fits the local bridge architecture.
3. Rebuild `~/.nanobot/bridge`.
4. Run focused Weixin tests plus bridge build.
5. Restart:
   - `nanobot-weixin-bridge.service`
   - `nanobot-gateway.service`
6. Verify:
   - text send/receive
   - inbound image forwarding
   - outbound media send
   - QR refresh
   - restart continuity via persisted `contextTokens`
    - daily digest Weixin broadcast still works via `channels.weixin.allowFrom`
