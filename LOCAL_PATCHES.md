## Local Maintenance Notes

Last updated: 2026-03-22

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

Key files to re-check after every upstream merge:

- `nanobot/agent/context.py`
- `nanobot/agent/loop.py`
- `nanobot/agent/memory.py`
- `nanobot/agent/semantic_memory.py`
- `nanobot/agent/tools/semantic_memory.py`
- `nanobot/config/schema.py`
- `nanobot/skills/memory/SKILL.md`
- `tests/test_semantic_memory.py`
- `tests/test_memory_consolidation_types.py`

### Related Local Changes Outside This Repo

- Daily digest isolation lives in:
  - `/home/Hera/.nanobot/workspace/services/daily-digest/daily_digest.py`
- That service must continue to pass:
  - `NANOBOT_DISABLE_SEMANTIC_MEMORY=1`

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
- Semantic embedding provider:
  - NVIDIA NIM via `NIM.key`
- Memory policy:
  - `MEMORY.md` should stay empty
  - `HISTORY.md` stays as archive only
  - volatile digest content must not be stored in Mem0
  - turn-level auto-capture is enabled locally for broad life-assistant memories
  - transient short-term states should not be auto-stored

### Upgrade Checklist

- Verify semantic recall still works on normal chat turns.
- Verify `memory_add` still rejects volatile content.
- Verify daily digest still runs with semantic memory disabled.
- Verify `~/.nanobot/workspace/skills/Codex-Listener` still resolves to the listener repo.
- Re-run the focused test suite before restarting services.
- Restart and check:
  - `nanobot-gateway`
  - `codex-listener`

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

- local-preferred merge using `git merge -X ours`
