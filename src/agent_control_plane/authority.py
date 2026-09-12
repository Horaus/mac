"""Deterministic supervisor authority primitives.

This module contains policy decisions only; prompts and provider adapters are
never used as an authorization mechanism.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Iterable


class ExecutionMode(str, Enum):
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    OPEN_OPERATOR = "open_operator"
    ISOLATED_SANDBOX = "isolated_sandbox"


CAPABILITIES = frozenset({
    "filesystem.read", "filesystem.write", "shell.execute", "git.worktree_create", "git.commit", "git.integrate", "git.cherry_pick_abort", "git.push",
    "browser.observe", "browser.navigate", "browser.reload_tab", "browser.reload_extension",
    "browser.restart", "app.observe", "app.mutate", "provider.preflight",
    "provider.free_submit", "provider.paid_submit", "destructive.delete", "external.publish", "dependency.install",
})


class MutationClass(str, Enum):
    OBSERVE = "observe"
    REVERSIBLE = "reversible_mutation"
    SHARED_RUNTIME = "shared_runtime_mutation"
    COSTLY_EXTERNAL = "costly_destructive_external"

class RuntimeOperation(str, Enum):
    OBSERVE = "observe"
    NAVIGATE = "navigate"
    RELOAD_TAB = "reload_tab"
    RELOAD_EXTENSION = "reload_extension"
    RESTART = "restart"


@dataclass(frozen=True)
class AuthorityPolicy:
    mode: ExecutionMode = ExecutionMode.ISOLATED_SANDBOX
    capabilities: frozenset[str] = frozenset({"filesystem.read", "provider.preflight"})

    def __post_init__(self):
        unknown = set(self.capabilities) - CAPABILITIES
        if unknown: raise ValueError(f"unknown capabilities: {', '.join(sorted(unknown))}")
        if self.mode == ExecutionMode.READ_ONLY and self.capabilities.intersection({
            "filesystem.write", "shell.execute", "git.commit", "git.integrate", "git.push",
            "app.mutate", "browser.navigate", "browser.reload_tab", "browser.reload_extension",
            "browser.restart", "provider.free_submit", "provider.paid_submit", "destructive.delete",
            "external.publish", "dependency.install",
        }):
            raise PermissionError("read-only execution mode cannot grant mutation capabilities")

    def snapshot(self):
        return {"mode": self.mode.value, "capabilities": sorted(self.capabilities)}

    def require(self, capability: str) -> None:
        if capability not in CAPABILITIES: raise ValueError(f"unknown capability: {capability}")
        if capability not in self.capabilities: raise PermissionError(f"capability not granted: {capability}")


@dataclass(frozen=True)
class ApprovalToken:
    token_id: str
    capability: str
    project_id: str
    provider_id: str
    job_id: str
    payload_hash: str
    idempotency_key: str
    max_attempts: int = 1
    attempts: int = 0
    expires_at: float = 0.0
    invalidated: bool = False
    run_id: str | None = None
    task_id: str | None = None

    def usable(self, policy: AuthorityPolicy, payload: object, now: float | None = None,
               project_id: str | None = None, provider_id: str | None = None,
               job_id: str | None = None, idempotency_key: str | None = None,
               run_id: str | None = None, task_id: str | None = None) -> bool:
        policy.require(self.capability)
        return (not self.invalidated and self.attempts < self.max_attempts and
                (self.expires_at <= 0 or (now or time.time()) < self.expires_at) and
                digest(payload) == self.payload_hash and
                (project_id is None or project_id == self.project_id) and
                (provider_id is None or provider_id == self.provider_id) and
                (job_id is None or job_id == self.job_id) and
                (idempotency_key is None or idempotency_key == self.idempotency_key) and
                (run_id is None or run_id == self.run_id) and (task_id is None or task_id == self.task_id))


@dataclass(frozen=True)
class RuntimeReport:
    pid: int | None = None
    executable: str = ""
    debug_port: int | None = None
    user_data_dir: str = ""
    target_id: str = ""
    target_url: str = ""
    extension_id: str = ""
    project_id: str = ""
    account_id: str = ""
    active_job: str = ""
    intended_action: str = ""
    recovery_path: str = ""
    allowed_target_ids: tuple[str, ...] = ()
    allowed_project_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextPacket:
    version: int
    objective: str
    current_state: dict
    authorizations: tuple[str, ...] = ()
    prohibited_actions: tuple[str, ...] = ()
    resources: tuple[str, ...] = ()
    leases: tuple[str, ...] = ()
    relevant_files: tuple[str, ...] = ()
    runtime: RuntimeReport | None = None
    acceptance_criteria: tuple[str, ...] = ()
    stop_conditions: tuple[str, ...] = ()
    response_schema: dict = field(default_factory=dict)

    def snapshot(self):
        value = asdict(self)
        if self.runtime: value["runtime"] = asdict(self.runtime)
        return value

    def digest(self): return digest(self.snapshot())


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def classify_mutation(capability: str, destructive: bool = False, external: bool = False) -> MutationClass:
    if destructive or external or capability in {"provider.paid_submit", "external.publish", "destructive.delete", "git.push"}:
        return MutationClass.COSTLY_EXTERNAL
    if capability in {"browser.restart", "app.mutate", "filesystem.write", "git.commit"}:
        return MutationClass.SHARED_RUNTIME
    if capability in {"browser.navigate", "browser.reload_tab", "browser.reload_extension", "shell.execute"}:
        return MutationClass.REVERSIBLE
    return MutationClass.OBSERVE


def validate_runtime_mutation(policy: AuthorityPolicy, capability: str, report: RuntimeReport | None,
                              active_job: bool = False, target_id: str | None = None,
                              project_id: str | None = None) -> None:
    policy.require(capability)
    if capability.startswith("browser.") and capability != "browser.observe" and report is None:
        raise ValueError("structured runtime report required before browser mutation")
    if report is not None and capability.startswith("browser.") and capability != "browser.observe":
        required = {"pid": report.pid, "executable": report.executable, "debug_port": report.debug_port,
                    "user_data_dir": report.user_data_dir, "target_id": report.target_id,
                    "target_url": report.target_url, "extension_id": report.extension_id,
                    "project_id": report.project_id, "account_id": report.account_id,
                    "intended_action": report.intended_action, "recovery_path": report.recovery_path}
        if any(value in (None, "") for value in required.values()):
            raise ValueError("runtime report is incomplete for browser mutation")
        if target_id and report.allowed_target_ids and target_id not in report.allowed_target_ids:
            raise PermissionError("target is outside runtime report scope")
        if project_id and report.allowed_project_ids and project_id not in report.allowed_project_ids:
            raise PermissionError("project is outside runtime report scope")
    if capability == "browser.restart" and active_job:
        raise RuntimeError("browser restart is forbidden during an active job")

def validate_runtime_operation(capability: str, operation: RuntimeOperation | str | None) -> None:
    if operation is None:
        raise PermissionError("structured runtime operation is required")
    try: op = RuntimeOperation(operation)
    except ValueError as error: raise PermissionError("unsupported runtime operation") from error
    expected = {"browser.observe": RuntimeOperation.OBSERVE, "browser.navigate": RuntimeOperation.NAVIGATE,
                "browser.reload_tab": RuntimeOperation.RELOAD_TAB, "browser.reload_extension": RuntimeOperation.RELOAD_EXTENSION,
                "browser.restart": RuntimeOperation.RESTART}
    if capability.startswith("browser.") and expected.get(capability) != op:
        raise PermissionError("runtime operation does not match capability")


class LoopGuard:
    def __init__(self, threshold: int = 3): self.threshold = threshold; self._events: dict[str, tuple[str, int]] = {}
    def record(self, blocker: str, external_state_version: str) -> bool:
        key = digest(blocker)
        old = self._events.get(key)
        count = old[1] + 1 if old and old[0] == external_state_version else 1
        self._events[key] = (external_state_version, count)
        return count >= self.threshold


def bound_output(output: str, limit: int, provenance: str) -> dict:
    if limit <= 0: raise ValueError("output limit must be positive")
    encoded = output.encode("utf-8")
    clipped = encoded[:limit]
    while True:
        try: text = clipped.decode("utf-8"); break
        except UnicodeDecodeError: clipped = clipped[:-1]
    omitted = max(0, len(encoded) - len(clipped))
    return {"text": text, "omitted_bytes": omitted, "provenance": provenance,
            "truncated": omitted > 0}
