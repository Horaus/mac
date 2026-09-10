"""Offline provider fixtures for deterministic usage/benchmark acceptance tests."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .providers import ProviderAdapter, WorkerResult
from .providers import parse_usage


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


class SessionFixtureAdapter(ProviderAdapter):
    """Offline provider-native continuity fixture; conversation history is ignored."""
    name = "session-fixture"

    def __init__(self):
        self.sessions: dict[str, str] = {}
        self.counter = 0

    def run(self, prompt: str, cwd: Path, profile=None, context_packet=None):
        self.counter += 1
        session = f"fixture-session-{self.counter}"
        marker = prompt.split("MARKER:", 1)[1].strip() if "MARKER:" in prompt else ""
        self.sessions[session] = marker
        return WorkerResult(0, marker, session, {"input_tokens": 1, "output_tokens": 1})

    def resume(self, session_id: str, prompt: str, cwd: Path, profile=None, resume_snapshot=False):
        adapter = self
        class Completed:
            def __init__(self):
                self.session_id = session_id
                self.cwd = Path(cwd)
                self.adapter = adapter
            def wait(self, timeout=None):
                if session_id not in adapter.sessions:
                    return WorkerResult(1, "provider session missing", session_id, {}, "SESSION_NOT_FOUND")
                return WorkerResult(0, adapter.sessions[session_id], session_id, {"input_tokens": 1, "output_tokens": 1})
        return Completed()


def benchmark_recorded_luna_sol() -> dict:
    """Comparable offline benchmark fixture; real account entitlement is not assumed."""
    return {name: RecordedProviderAdapter().turn.__dict__.copy() for name in ("luna", "sol")}


def budget_fixture_matrix() -> dict:
    """Recorded observations used to distinguish per-event from cumulative usage."""
    return {
        "event_4k": '{"usage":{"input_tokens":4000}}',
        "event_12k": '{"usage":{"input_tokens":12000}}',
        "event_16k": '{"usage":{"input_tokens":16000}}',
        "cumulative_12k_to_650k": "\n".join(
            '{"cumulative":true,"usage":{"input_tokens":%d}}' % value
            for value in (12000, 180000, 650000)
        ),
    }


def parsed_budget_fixture_matrix() -> dict:
    return {name: parse_usage(value)["input_tokens"] for name, value in budget_fixture_matrix().items()}
