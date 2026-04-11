## Local Maintenance Notes

Last updated: 2026-04-11

This file tracks local behavior that intentionally diverges from upstream so future upgrades can preserve it.

### Current Goals

- Use Mem0 as the primary long-term memory store.
- Keep `memory/MEMORY.md` empty and out of the normal prompt path.
- Keep `memory/HISTORY.md` as a lazy archive only.
- Isolate daily news/weather workflows from semantic memory.

### Local Changes In `nanobot`

- Added NVIDIA NIM backed semantic memory via Mem0.
- Added `memory_add` and `memory_search` tools.
- Inject semantic recall into normal chat turns.
- Disabled automatic long-term writes to Markdown memory in the current local policy.
- Kept `HISTORY.md` archival behavior but disabled automatic history-to-Mem0 syncing in the current local policy.
- Added volatile-content filtering so news, weather, forecasts, system status, and similar summaries do not enter Mem0.
- Added `NANOBOT_DISABLE_SEMANTIC_MEMORY=1` support to hard-disable semantic memory for selected processes.
- Added automatic turn-level memory capture for durable user facts/preferences, with Mem0 dedupe and a lightweight `（我记下了）` hint after successful writes.
- Explicit `memory_add` now suppresses same-turn auto-capture so one fact is not written twice.
- Local `pyproject.toml` keeps the Mem0 runtime dependencies so rebuilds and restore flows do not silently drop semantic memory support.
- Chat channels (`weixin`, `telegram`, `telegram_planbridge`) now only inject runtime time after 10 minutes of idle, and include a human-readable idle-gap hint once that threshold is crossed.
- Switched the default chat provider to the AxonHub OpenAI-compatible endpoint using `provider=custom`, model `ollama/kimi-k2.5`, and base URL `https://axon.061609.xyz/v1`.
- Kept Weixin on upstream's direct HTTP long-poll channel and removed the standalone bridge runtime from the local stack.
- Weixin runtime state now lives under `~/.nanobot/weixin/account.json`; legacy `~/.nanobot/weixin-auth` is retained only as a migration/rollback source.
- Added `nanobot/utils/weixin_state_migration.py` plus `scripts/migrate_weixin_state.py` to convert one saved legacy bridge account into direct-channel state.
- Added `mirrorWeixinAllowFrom` as a local cron payload field so curated background shares can keep Telegram as the primary target while best-effort mirroring the same final text to current `channels.weixin.allowFrom`.
- Extracted the Weixin allowFrom broadcast path into `nanobot/utils/weixin_broadcast.py`, shared by both the daily digest service and curated cron share callbacks, now using direct-channel `account.json` plus persisted `context_tokens`.
- Added a local `bilibili_daily_share` content-source integration backed by `~/.nanobot/workspace/skills/bilibili-daily-share/`, login state in `~/.nanobot/bilibili-auth/`, and a dedicated `~/.nanobot/venvs/bilibili-cli` runtime pinned to `bilibili-api-python==17.4.1`.
- The Bilibili daily share path intentionally uses logged-in homepage recommendations rather than any public hot list, and only sends Telegram login reminders when auth expires.
- Curated Mastodon share marking keeps a local fallback path that can extract canonical status IDs directly from the final response URL when `last_prepare.json` is missing.

Key files to re-check after every upstream merge:

- `nanobot/agent/context.py`
- `nanobot/agent/loop.py`
- `nanobot/agent/memory.py`
- `nanobot/agent/runner.py`
- `nanobot/agent/semantic_memory.py`
- `nanobot/agent/tools/semantic_memory.py`
- `nanobot/channels/telegram.py`
- `nanobot/channels/weixin.py`
- `nanobot/cli/commands.py`
- `nanobot/cron/service.py`
- `nanobot/utils/weixin_broadcast.py`
- `nanobot/utils/weixin_state_migration.py`
- `nanobot/config/schema.py`
- `nanobot/skills/memory/SKILL.md`
- `tests/test_semantic_memory.py`
- `tests/channels/test_telegram_channel.py`
- `tests/channels/test_weixin_channel.py`

### Related Local Changes Outside This Repo

- Daily digest isolation lives in:
  - `/home/Hera/.nanobot/workspace/services/daily-digest/daily_digest.py`
- That service must continue to pass:
  - `NANOBOT_DISABLE_SEMANTIC_MEMORY=1`
- Daily digest now broadcasts the same weather text to Weixin recipients from `channels.weixin.allowFrom` in addition to Telegram, and the timer target is 06:30 CST.
- Curated random social shares also use the same Weixin allowFrom broadcast helper, but only for their final share text and never for progress updates or Bilibili login reminders.
- Service-level provider secrets live in:
  - `/etc/default/nanobot`
- Weixin maintenance notes live in:
  - `docs/weixin-bridge-maintenance.md`
- Current external secret files in the home directory:
  - `~/NIM.key` for NVIDIA NIM embeddings
  - `~/OLLAMA_CLOUD.key` as the archived retired Ollama Cloud key
  - `~/axonhub.key` for the active AxonHub API key
- Current local auth state directories in the home directory:
  - `~/.nanobot/weixin`
  - `~/.nanobot/weixin-auth` (legacy migration source only)
  - `~/.nanobot/bilibili-auth`
  - `~/.config/toot`

### Codex-Listener Dependency

- `~/.nanobot/workspace/skills/Codex-Listener` is a symlink to:
  - `/opt/ai-stack/codex-listener/skills/Codex-Listener`
- Local listener customizations should be checked after upgrades, especially:
  - `skills/Codex-Listener/SKILL.md`
  - `skills/Codex-Listener/scripts/submit.py`
  - `src/codex_listener/channels/telegram.py`
  - `src/codex_listener/task_manager.py`
  - `pyproject.toml`

### Configuration Conventions

- Semantic memory global kill switch:
  - `NANOBOT_DISABLE_SEMANTIC_MEMORY=1`
- Default chat provider:
  - `agents.defaults.provider` must stay `custom`
  - `agents.defaults.model` must stay `ollama/kimi-k2.5`
  - `providers.custom.apiBase` must stay `https://axon.061609.xyz/v1`
  - Do not switch back to `auto` while using the `ollama/...` model name, or nanobot may incorrectly resolve to the built-in local Ollama provider.
- Provider secrets:
  - `providers.vllm.apiKey` should remain absent from `~/.nanobot/config.json`
  - `NANOBOT_PROVIDERS__CUSTOM__API_KEY` should be sourced from `/etc/default/nanobot`
- Semantic embedding provider:
  - NVIDIA NIM via `NIM.key`
- Memory policy:
  - `MEMORY.md` should stay empty
  - `HISTORY.md` stays as archive only
  - volatile digest content must not be stored in Mem0
  - turn-level auto-capture is enabled locally for broad life-assistant memories
  - explicit `memory_add` wins over same-turn auto-capture
  - transient short-term states should not be auto-stored

### Upgrade Checklist

- Verify semantic recall still works on normal chat turns.
- Verify `memory_add` still rejects volatile content.
- Verify daily digest still runs with semantic memory disabled.
- Verify curated random shares still only send final text, never progress, while mirroring to Weixin when `mirrorWeixinAllowFrom=true`.
- Verify `bilibili_daily_share` still reads logged-in homepage recommendations, keeps `last_prepare.json` in sync with callback matching, and only emits Telegram login reminders on auth expiry.
- Verify `~/.nanobot/workspace/skills/Codex-Listener` still resolves to the listener repo.
- Verify the local Weixin direct channel still preserves:
  - `context_tokens` persistence inside `~/.nanobot/weixin/account.json`
  - legacy state migration from `~/.nanobot/weixin-auth`
  - session-expired handling without noisy retry loops
  - optional `routeTag` compatibility for ilinkai 1.0.3+
  - QR refresh behavior
  - inbound image parsing via Tencent official `item_list + CDN decrypt` semantics
  - outbound media sending for image / video / file
- Re-run the focused test suite before restarting services.
- Restart and check:
  - `nanobot-gateway`
  - `codex-listener`

### 2026-04-11 Upstream Merge Record

- Current upstream merge target:
  - `origin/main` at `9bccfa6`
- Local integration branch used for conflict resolution:
  - `integrate-20260411-origin-main`
- This upgrade intentionally switched away from blanket `-X ours`.
- Current merge policy:
  - merge-based upgrade with manual conflict resolution
  - preserve the local memory / provider / Weixin / curated-share behavior listed above
  - absorb upstream runtime correctness, cron, tool-hint, websocket, unified-session, and Telegram transport fixes where they do not change local defaults
- Highest-risk overlap areas in this cycle:
  - `nanobot/agent/loop.py`
  - `nanobot/agent/memory.py`
  - `nanobot/agent/runner.py`
  - `nanobot/channels/telegram.py`
  - `nanobot/cli/commands.py`
  - `nanobot/config/schema.py`
  - `nanobot/cron/service.py`
  - `nanobot/utils/helpers.py`
  - `tests/agent/test_unified_session.py`
  - `tests/cron/test_cron_service.py`

### 2026-03-24 Backup And Update Record

- Runtime backup directory:
  - `/home/Hera/.nanobot/backup/update-20260324-190605`
- System packages refreshed:
  - `openssl` / `libssl*`
  - `openssh-*`
  - `libc6` / `libc-bin`
  - `nodejs` / `libnode*`
  - `tzdata`
- Upstream merge target refreshed to:
  - `origin/main` at `72acba5`

### 2026-03-22 Backup And Update Record

GitHub backup branches pushed under `jacob-sheng`:

- `nanobot`: `backup-20260322-1558-nanobot-local` at `c0399a7`
- `codex-listener`: `backup-20260322-1558-codex-listener-local` at `978e9a4`

Starting local branches:

- `nanobot`: `upgrade-v0.1.4.post5-20260319172534`
- `codex-listener`: `safe-update-20260308120139-codex-listener`

Current integration branches:

- `nanobot`: `update-20260322-hkuds-main`
- `codex-listener`: `update-20260322-talexck-master`

Upstream merge targets:

- `nanobot`: `origin/main`
- `codex-listener`: `origin/master`

Merge policy:

- retired after the 2026-04-11 upstream merge
