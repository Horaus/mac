"""Provider-output extraction for compact supervisor-facing results."""
from __future__ import annotations

import json


def compact_result_summary(output: str, byte_limit: int = 2000) -> str:
    """Return the last terminal assistant message, never command output."""
    assistant_messages: list[str] = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if (event.get("type") == "item.completed" and isinstance(item, dict)
                and item.get("type") == "agent_message" and isinstance(item.get("text"), str)):
            assistant_messages.append(item["text"].strip())
    value = assistant_messages[-1] if assistant_messages else output.encode("utf-8")[-byte_limit:].decode("utf-8", errors="ignore").strip()
    encoded = value.encode("utf-8")
    return value if len(encoded) <= byte_limit else encoded[:byte_limit].decode("utf-8", errors="ignore")
