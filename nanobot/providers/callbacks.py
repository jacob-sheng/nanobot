"""LiteLLM callbacks for conversation tracing.

This module provides a non-invasive way to capture complete LLM conversation
traces, including agent and subagent trajectories, without modifying core
agent loop logic.

The callback receives kwargs["messages"] which contains the FULL conversation
history sent to the LLM - this is exactly what we need for trajectory persistence.

Reference: https://docs.litellm.ai/docs/observability/custom_callback
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from litellm.integrations.custom_logger import CustomLogger
from loguru import logger


def _calc_duration_ms(start_time: Any, end_time: Any) -> int:
    """Calculate duration in milliseconds, handling both float and datetime inputs."""
    try:
        diff = end_time - start_time
        if isinstance(diff, timedelta):
            return int(diff.total_seconds() * 1000)
        return int(diff * 1000)
    except (TypeError, AttributeError):
        return 0


class ConversationCallback(CustomLogger):
    """LiteLLM callback for tracking full conversation traces.

    Captures complete LLM call context including:
    - kwargs["messages"]: Full conversation history (user + assistant + tool)
    - Response content, model, usage stats
    - Session metadata via kwargs["litellm_params"]["metadata"]

    This enables trajectory persistence without invasive modifications to
    agent loop or subagent code.

    Usage:
        callback = ConversationCallback(jsonl_path=Path("traces.jsonl"))

        # Register with LiteLLM (in LiteLLMProvider.__init__):
        litellm.callbacks = [callback]

        # Or pass via kwargs (current pattern in LiteLLMProvider.chat):
        kwargs.setdefault("callbacks", []).append(callback)

    Metadata Fields (passed via litellm_params.metadata):
        - session_key: Session identifier (e.g., "cli:direct", "subagent:abc123")
        - agent_type: "main" or "subagent"
        - parent_session: For subagents, the parent session key
        - task_id: For subagents, the task identifier
        - turn_count: Current turn number in the conversation
    """

    def __init__(self, jsonl_path: str | Path | None = None):
        super().__init__()
        self.jsonl_path = Path(jsonl_path) if jsonl_path else None

    async def async_log_success_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: float,
        end_time: float,
    ) -> None:
        """Called by LiteLLM after each successful LLM completion.

        Args:
            kwargs: Contains model, messages, litellm_params (with metadata), etc.
            response_obj: The completion response object
            start_time: Unix timestamp of call start
            end_time: Unix timestamp of call end
        """
        try:
            # Extract core data
            messages = kwargs.get("messages", [])
            model = kwargs.get("model", "unknown")

            # Extract metadata (session correlation)
            litellm_params = kwargs.get("litellm_params", {})
            metadata = litellm_params.get("metadata", {})

            # Extract response content
            response_content = ""
            finish_reason = ""
            if hasattr(response_obj, "choices") and response_obj.choices:
                choice = response_obj.choices[0]
                msg = choice.message
                response_content = getattr(msg, "content", "") or ""
                finish_reason = getattr(choice, "finish_reason", "") or ""

            # Extract usage stats
            usage = {}
            if hasattr(response_obj, "usage") and response_obj.usage:
                u = response_obj.usage
                usage = {
                    "prompt_tokens": getattr(u, "prompt_tokens", 0),
                    "completion_tokens": getattr(u, "completion_tokens", 0),
                    "total_tokens": getattr(u, "total_tokens", 0),
                }

            # Extract cost and cache info (LiteLLM calculates these)
            cost = kwargs.get("response_cost", 0)
            cache_hit = kwargs.get("cache_hit", False)

            # Build trace entry
            entry = {
                "timestamp": datetime.now().isoformat(),
                "model": model,
                "messages": messages,  # FULL conversation history
                "response": response_content,
                "finish_reason": finish_reason,
                "usage": usage,
                "cost": cost,
                "cache_hit": cache_hit,
                "metadata": metadata,
                "start_time": start_time.isoformat() if isinstance(start_time, datetime) else start_time,
                "end_time": end_time.isoformat() if isinstance(end_time, datetime) else end_time,
                "duration_ms": _calc_duration_ms(start_time, end_time),
            }

            # Write to JSONL
            if self.jsonl_path:
                self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.jsonl_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")

            logger.debug(
                "ConversationCallback: session={}, model={}, messages={}, cost={}",
                metadata.get("session_key", "unknown"),
                model,
                len(messages),
                cost
            )

        except Exception as e:
            logger.warning("ConversationCallback error: {}", e)

    async def async_log_failure_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: float,
        end_time: float,
    ) -> None:
        """Called by LiteLLM when a completion fails."""
        try:
            model = kwargs.get("model", "unknown")
            litellm_params = kwargs.get("litellm_params", {})
            metadata = litellm_params.get("metadata", {})
            exception = kwargs.get("exception", None)

            error_entry = {
                "timestamp": datetime.now().isoformat(),
                "event_type": "failure",
                "model": model,
                "metadata": metadata,
                "error": str(exception) if exception else "Unknown error",
                "error_type": type(exception).__name__ if exception else "Unknown",
                "start_time": start_time.isoformat() if isinstance(start_time, datetime) else start_time,
                "end_time": end_time.isoformat() if isinstance(end_time, datetime) else end_time,
                "duration_ms": _calc_duration_ms(start_time, end_time),
            }

            if self.jsonl_path:
                self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.jsonl_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(error_entry, ensure_ascii=False) + "\n")

            logger.warning(
                "ConversationCallback failure: session={}, model={}, error={}",
                metadata.get("session_key", "unknown"),
                model,
                exception
            )

        except Exception as e:
            logger.error("ConversationCallback failure logging error: {}", e)

    def __repr__(self) -> str:
        return f"ConversationCallback(jsonl_path={self.jsonl_path})"
