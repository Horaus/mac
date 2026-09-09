"""Offline provider fixtures for deterministic usage/benchmark acceptance tests."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .providers import ProviderAdapter, WorkerResult


@dataclass(frozen=True)
class RecordedTurn:
    output: str = "recorded output"
    input_tokens: int = 100
    cached_input_tokens: int = 20
    output_tokens: int = 12
    files_read: int = 3
    commands_executed: int = 2
    tool_output_bytes: int = 64


class RecordedProviderAdapter(ProviderAdapter):
    name = "recorded-provider"

    def __init__(self, turn: RecordedTurn = RecordedTurn()):
        self.turn = turn

    def run(self, prompt: str, cwd: Path, profile=None):
        limit = getattr(getattr(profile, "context", None), "max_input_tokens", None)
        if limit is not None and self.turn.input_tokens > limit:
            return WorkerResult(2, "hard input budget denied", "recorded-denied",
                                {"input_tokens": self.turn.input_tokens,
                                 "cached_input_tokens": self.turn.cached_input_tokens,
                                 "output_tokens": 0, "files_read": 0,
                                 "commands_executed": 0, "tool_output_bytes": 0},
                                "hard_budget_denied")
        return WorkerResult(0, self.turn.output, "recorded-session",
                            {"input_tokens": self.turn.input_tokens,
                             "cached_input_tokens": self.turn.cached_input_tokens,
                             "output_tokens": self.turn.output_tokens,
                             "files_read": self.turn.files_read,
                             "commands_executed": self.turn.commands_executed,
                             "tool_output_bytes": self.turn.tool_output_bytes}, None)


def benchmark_recorded_luna_sol() -> dict:
    """Comparable offline benchmark fixture; real account entitlement is not assumed."""
    return {name: RecordedProviderAdapter().turn.__dict__.copy() for name in ("luna", "sol")}
