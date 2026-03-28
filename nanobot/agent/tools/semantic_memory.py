"""Tools for explicit semantic memory add/search operations."""

from __future__ import annotations

from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.agent.semantic_memory import SemanticMemory


class MemoryAddTool(Tool):
    """Persist explicit long-term memories into the semantic store."""

    def __init__(self, semantic_memory: SemanticMemory):
        self.semantic_memory = semantic_memory

    @property
    def name(self) -> str:
        return "memory_add"

    @property
    def description(self) -> str:
        return "Save an explicit long-term memory for later semantic recall."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The fact, preference, or note to remember.",
                    "minLength": 1,
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional labels for later filtering or audit.",
                },
            },
            "required": ["text"],
        }

    async def execute(self, text: str, tags: list[str] | None = None, **kwargs: Any) -> str:
        cleaned = text.strip()
        if not cleaned:
            return "Error: text must not be empty"

        metadata = {"source": "tool"}
        if tags:
            metadata["tags"] = [tag for tag in tags if tag]

        if not self.semantic_memory.should_store_text(cleaned, metadata=metadata):
            return "Refused: volatile content (news/weather/status)."

        result = await self.semantic_memory.add_text(cleaned, metadata=metadata, role="user")
        try:
            await self.semantic_memory.append_audit_entry(cleaned, tags=tags)
        except Exception:
            pass

        count = len(result.get("results", [])) if isinstance(result, dict) else 0
        return f"Saved semantic memory ({count} item{'s' if count != 1 else ''})."


class MemorySearchTool(Tool):
    """Search semantic memory without modifying the store."""

    def __init__(self, semantic_memory: SemanticMemory):
        self.semantic_memory = semantic_memory

    @property
    def name(self) -> str:
        return "memory_search"

    @property
    def description(self) -> str:
        return "Search previously stored semantic memories for relevant notes."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to search for in semantic memory.",
                    "minLength": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results to return.",
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str, limit: int | None = None, **kwargs: Any) -> str:
        results = await self.semantic_memory.search(query, limit=limit)
        if not results:
            return "No semantic memory matches found."

        lines = []
        for idx, item in enumerate(results, start=1):
            memory = str(item.get("memory") or "").strip()
            if not memory:
                continue
            score = item.get("score")
            score_text = f" (score {score:.2f})" if isinstance(score, (int, float)) else ""
            lines.append(f"{idx}. {memory}{score_text}")
        return "\n".join(lines) if lines else "No semantic memory matches found."
