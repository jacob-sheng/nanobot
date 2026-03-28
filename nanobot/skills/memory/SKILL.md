---
name: memory
description: Two-layer memory system with grep-based recall.
always: true
---

# Memory

## Structure

- `memory/MEMORY.md` — Legacy Markdown memory. It may be disabled or left empty when semantic memory is the primary store.
- `memory/HISTORY.md` — Legacy append-only event log. It should be treated as on-demand data, not something to read eagerly.
- Semantic memory — Use `memory_add` and `memory_search` as the primary long-term memory interface when available.

## Search Past Events

Choose the search method based on file size:

- Small `memory/HISTORY.md`: use `read_file`, then search in-memory
- Large or long-lived `memory/HISTORY.md`: use the `exec` tool for targeted search

Examples:
- **Linux/macOS:** `grep -i "keyword" memory/HISTORY.md`
- **Windows:** `findstr /i "keyword" memory\HISTORY.md`
- **Cross-platform Python:** `python -c "from pathlib import Path; text = Path('memory/HISTORY.md').read_text(encoding='utf-8'); print('\n'.join([l for l in text.splitlines() if 'keyword' in l.lower()][-20:]))"`

Prefer targeted command-line search for large history files.

## Preferred Memory Writes

Prefer `memory_add` for important facts:
- User preferences ("I prefer dark mode")
- Project context ("The API uses OAuth2")
- Relationships ("Alice is the project lead")
- Stable personal facts ("My name is 阿钖")

Do not store short-lived clutter in long-term memory:
- Daily news digests, headline summaries, or article roundups
- Weather forecasts, temperature snapshots, or system-health/status snapshots
- Temporary operational output that will age out quickly

Only edit `MEMORY.md` directly when semantic memory is unavailable or the user explicitly wants Markdown files updated.

When the user asks what you remember or wants a semantic lookup across old notes, you can use the `memory_search` tool.

## Auto-consolidation

Old conversations may still be consolidated into `memory/HISTORY.md` for archival purposes.

Treat `HISTORY.md` as lazy, on-demand context rather than eager prompt context, and do not mirror daily archives into semantic memory unless the user explicitly wants that behavior.
