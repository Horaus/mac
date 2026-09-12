"""Minimal stdio JSON-RPC/MCP-compatible facade for local supervisor tooling."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .store import Store
from .providers import provider
from .service import (arbitrate_conflict, cancel_managed_worker, finish_managed_worker,
                       pause_managed_worker, resume_live_worker, run_worker,
                       start_managed_worker, resume_managed_worker, validate, authorize_action, authorize_runtime_action, report_blocker,
                       submit_provider, publish_external, delete_destructive, _dependency_preflight)
from .service import _inject_context, _restricted_context
from .service import HOST_INSTANCE_ID
from .orchestrator import Supervisor
from .profiles import resolve_profile, profile_from
from .authority import ApprovalToken, AuthorityPolicy, ContextPacket, ExecutionMode, digest
from .provisioning import DependencyContract, verify_offline
from .managed_scheduler import ManagedJob, ManagedScheduler
from .organization import (append_goal_event, assign_archetype, checkpoint as goal_checkpoint,
                            create_archetype, create_goal as org_create_goal, promote_memory,
                            propose_memory, recommend_model, recommend_worker, record_observation, set_inspection_mode,
                            compile_worker_pack, retrieve_memory, garbage_collect_memory, garbage_collect_conversation, garbage_collect_evidence, physically_collect_payloads,
                            add_evidence, acknowledge_goal, cancel_goal, follow_up, record_feedback, get_evidence, resume_goal, set_evidence_hold, subscribe_goal, poll_goal,
                            register_model_profile, register_skill, validate_worker_components, evaluate_budget, record_budget_telemetry, inspect_archetype, set_archetype_enabled,
                            register_rule,
                            summary as goal_summary)
from .organization_runtime import OrganizationDaemon, RecordedFreeProvider, configure_free_routing, configure_provider_eligibility, free_route, record_quota, register_credential_ref
from .service_fixtures import ProductionServiceManager, SandboxedServiceManager, subprocess_service_executor


TOOL_DEFS = {
    "status": {"task_id": {"type": "string"}, "inspection_mode": {"type": "string", "enum": ["result_only", "full"]}, "byte_limit": {"type": "number"}}, "inbox": {}, "reconcile": {}, "managed_queue_status": {},
    "control_status": {"task_id": {"type": "string"}, "inspection_mode": {"type": "string", "enum": ["result_only", "full"]}, "byte_limit": {"type": "number"}},
    "control_set_policy": {"policy": {"type": "string"}},
    "control_register_master": {"master_id": {"type": "string"}, "name": {"type": "string"}, "min_workers": {"type": "number"}, "max_workers": {"type": "number"}},
    "control_request_master_workers": {"master_id": {"type": "string"}, "requested": {"type": "number"}},
    "control_release_master": {"master_id": {"type": "string"}},
    "control_register_boss": {"boss_id": {"type": "string"}, "name": {"type": "string"}, "min_workers": {"type": "number"}, "max_workers": {"type": "number"}},
    "control_request_workers": {"boss_id": {"type": "string"}, "requested": {"type": "number"}},
    "control_release_boss": {"boss_id": {"type": "string"}},
    "history_add": {"conversation_id": {"type": "string"}, "role": {"type": "string"}, "actor": {"type": "string"}, "content": {"type": "string"}, "task_id": {"type": "string"}, "worker_id": {"type": "string"}},
    "history_list": {"conversation_id": {"type": "string"}, "limit": {"type": "number"}},
    "knowledge_load": {"path": {"type": "string"}},
    "knowledge_ack": {"actor": {"type": "string"}, "worker_id": {"type": "string"}},
    "declare_resource": {"name": {"type": "string"}, "kind": {"type": "string"}, "paths": {"type": "array", "items": {"type": "string"}}},
    "acquire_resource": {"resource": {"type": "string"}, "task_id": {"type": "string"}, "worker_id": {"type": "string"}, "mode": {"type": "string"}, "ttl": {"type": "number"}},
    "renew_resource": {"resource": {"type": "string"}, "task_id": {"type": "string"}, "worker_id": {"type": "string"}, "ttl": {"type": "number"}},
    "bump_resource": {"resource": {"type": "string"}},
    "accept_integration": {"task_id": {"type": "string"}, "worktree": {"type": "string"}, "commit_message": {"type": "string"}, "validation": {"type": "array", "items": {"type": "string"}}},
    "push_integration": {"remote": {"type": "string"}, "branch": {"type": "string"}, "token_id": {"type": "string"}, "project_id": {"type": "string"}, "provider_id": {"type": "string"}, "job_id": {"type": "string"}, "idempotency_key": {"type": "string"}, "run_id": {"type": "string"}, "task_id": {"type": "string"}, "execution": {"type": "object"}},
    "provider_submit": {"provider": {"type": "string"}, "job_id": {"type": "string"}, "payload": {"type": "object"}, "paid": {"type": "boolean"}, "approval": {"type": "object"}, "execution": {"type": "object"}},
    "external_publish": {"payload": {"type": "object"}, "approval": {"type": "object"}, "execution": {"type": "object"}},
    "destructive_delete": {"target": {"type": "string"}, "approval": {"type": "object"}, "execution": {"type": "object"}},
    "arbitrate_conflict": {"task_id": {"type": "string"}, "decision_id": {"type": "string"}, "provider": {"type": "string"}, "evidence": {"type": "string"}, "cwd": {"type": "string"}},
    "create_task": {"id": {"type": "string"}, "title": {"type": "string"}, "provider": {"type": "string"}, "goal_id": {"type": "string"}, "depends_on": {"type": "array", "items": {"type": "string"}}, "execution": {"type": "object"}},
    "create_resource": {"name": {"type": "string"}, "kind": {"type": "string"}, "paths": {"type": "array", "items": {"type": "string"}}},
    "send_message": {"type": {"type": "string"}, "payload": {"type": "object"}, "task_id": {"type": "string"}, "worker_id": {"type": "string"}},
    "accept_task": {"task_id": {"type": "string"}}, "pause_task": {"task_id": {"type": "string"}, "reason": {"type": "string"}},
    "resume_task": {"task_id": {"type": "string"}}, "cancel_task": {"task_id": {"type": "string"}, "reason": {"type": "string"}},
    "resolve_conflict": {"decision_id": {"type": "string"}, "task_id": {"type": "string"}, "decision": {"type": "string"}, "reason": {"type": "string"}},
    "run_worker": {"task_id": {"type": "string"}, "worker_id": {"type": "string"}, "provider": {"type": "string"}, "prompt": {"type": "string"}, "cwd": {"type": "string"}, "conversation_id": {"type": "string"}, "resume_session_id": {"type": "string"}, "resume_from_task_id": {"type": "string"}, "context_checkpoint": {"type": "object"}, "execution": {"type": "object", "properties": {"mode": {"type": "string", "enum": [mode.value for mode in ExecutionMode]}, "capabilities": {"type": "array", "items": {"type": "string"}}}}, "wait": {"type": "boolean"}},
    "start_worker": {"task_id": {"type": "string"}, "worker_id": {"type": "string"}, "provider": {"type": "string"}, "prompt": {"type": "string"}, "cwd": {"type": "string"}, "conversation_id": {"type": "string"}, "resume_session_id": {"type": "string"}, "context_checkpoint": {"type": "object"}, "execution": {"type": "object", "properties": {"mode": {"type": "string", "enum": [mode.value for mode in ExecutionMode]}, "capabilities": {"type": "array", "items": {"type": "string"}}}}, "rules": {"type": "array", "items": {"type": "object"}}, "skills": {"type": "array", "items": {"type": "object"}}, "resources": {"type": "array", "items": {"type": "object", "required": ["resource"], "properties": {"resource": {"type": "string"}, "mode": {"type": "string", "enum": ["READ", "WRITE"]}}}}},
    "pause_worker": {"task_id": {"type": "string"}, "worker_id": {"type": "string"}},
    "resume_worker": {"task_id": {"type": "string"}, "worker_id": {"type": "string"}},
    "wait_worker": {"task_id": {"type": "string"}, "worker_id": {"type": "string"}},
    "cancel_worker": {"task_id": {"type": "string"}, "worker_id": {"type": "string"}, "reason": {"type": "string"}},
    "validate": {"task_id": {"type": "string"}, "command": {"type": "string"}, "cwd": {"type": "string"}, "timeout": {"type": "number"}, "execution": {"type": "object"}},
    "create_approval": {"token_id": {"type": "string"}, "capability": {"type": "string"}, "project_id": {"type": "string"}, "provider_id": {"type": "string"}, "job_id": {"type": "string"}, "run_id": {"type": "string"}, "task_id": {"type": "string"}, "payload": {"type": "object"}, "idempotency_key": {"type": "string"}, "max_attempts": {"type": "number"}, "expires_at": {"type": "number"}},
    "consume_approval": {"token_id": {"type": "string"}, "capability": {"type": "string"}, "project_id": {"type": "string"}, "provider_id": {"type": "string"}, "job_id": {"type": "string"}, "run_id": {"type": "string"}, "task_id": {"type": "string"}, "idempotency_key": {"type": "string"}, "payload": {"type": "object"}},
    "authorize_action": {"capability": {"type": "string"}, "payload": {"type": "object"}, "token_id": {"type": "string"}, "destructive": {"type": "boolean"}, "external": {"type": "boolean"}, "project_id": {"type": "string"}, "provider_id": {"type": "string"}, "job_id": {"type": "string"}, "idempotency_key": {"type": "string"}, "run_id": {"type": "string"}, "task_id": {"type": "string"}, "execution": {"type": "object"}},
    "authorize_runtime_action": {"capability": {"type": "string"}, "report": {"type": "object"}, "resource": {"type": "string"}, "task_id": {"type": "string"}, "run_id": {"type": "string"}, "worker_id": {"type": "string"}, "active_job": {"type": "boolean"}, "target_id": {"type": "string"}, "project_id": {"type": "string"}, "operation": {"type": "string", "enum": ["observe", "navigate", "reload_tab", "reload_extension", "restart"]}, "execution": {"type": "object"}},
    "report_blocker": {"task_id": {"type": "string"}, "blocker": {"type": "string"}, "external_state_version": {"type": "string"}, "threshold": {"type": "number"}},
    "review_evidence": {"task_id": {"type": "string"}},
    "run_activity": {"run_id": {"type": "string"}},
    "heartbeat": {"run_id": {"type": "string"}, "action": {"type": "string"}},
    "queue_steering": {"run_id": {"type": "string"}, "instruction": {"type": "string"}, "scope": {"type": "string"}},
    "refresh_authority": {"run_id": {"type": "string"}, "execution": {"type": "object"}, "context": {"type": "object"}},
    "dependency_check": {"task_id": {"type": "string"}, "contract": {"type": "object"}, "store": {"type": "string"}},
    "org_create_goal": {"goal_id": {"type": "string"}, "title": {"type": "string"}, "owner": {"type": "string"}, "worker_class": {"type": "string", "enum": ["basic", "specialist"]}, "specialist_id": {"type": "string"}, "worker_profile": {"type": "object"}, "worker_pack": {"type": "string"}, "permissions": {"type": "array"}, "acceptance_criteria": {"type": "array"}, "inspection_mode": {"type": "string"}, "checkpoint_policy": {"type": "object"}},
    "org_goal_summary": {"goal_id": {"type": "string"}, "cursor": {"type": "number"}, "limit": {"type": "number"}, "fields": {"type": "array"}},
    "org_goal_event": {"goal_id": {"type": "string"}, "kind": {"type": "string"}, "payload": {"type": "object"}, "actor": {"type": "string"}},
    "org_checkpoint": {"goal_id": {"type": "string"}, "state": {"type": "object"}, "evidence": {"type": "array"}, "next_action": {"type": "string"}, "recoverability": {"type": "string"}, "owner": {"type": "string"}},
    "org_set_inspection": {"goal_id": {"type": "string"}, "mode": {"type": "string"}, "actor": {"type": "string"}},
    "org_propose_memory": {"goal_id": {"type": "string"}, "content": {"type": "string"}, "scope": {"type": "string"}, "source": {"type": "string"}, "owner": {"type": "string"}, "metadata": {"type": "object"}},
    "org_promote_memory": {"memory_id": {"type": "string"}, "scope": {"type": "string"}, "actor": {"type": "string"}},
    "org_create_archetype": {"archetype_id": {"type": "string"}, "version": {"type": "number"}, "name": {"type": "string"}, "role": {"type": "string"}, "owner": {"type": "string"}, "taxonomy": {"type": "array"}, "skills": {"type": "array"}, "rules": {"type": "array"}},
    "org_assign_archetype": {"goal_id": {"type": "string"}, "instance_id": {"type": "string"}, "archetype_id": {"type": "string"}, "version": {"type": "number"}, "actor": {"type": "string"}},
    "org_inspect_archetype": {"archetype_id": {"type": "string"}, "version": {"type": "number"}},
    "org_set_archetype_enabled": {"archetype_id": {"type": "string"}, "version": {"type": "number"}, "enabled": {"type": "boolean"}, "actor": {"type": "string"}},
    "org_observe": {"observation": {"type": "object"}},
    "org_recommend_model": {"taxonomy": {"type": "string"}, "minimum_samples": {"type": "number"}, "dimensions": {"type": "object"}},
    "org_recommend_worker": {"goal": {"type": ["object", "string"]}, "risk": {"type": "string"}, "constraints": {"type": "object"}},
    "org_compile_pack": {"pack_id": {"type": "string"}, "goal_id": {"type": "string"}, "owner": {"type": "string"}, "components": {"type": "object"}},
    "org_evaluate_budget": {"policy": {"type": "object"}, "telemetry": {"type": "object"}},
    "org_record_budget_telemetry": {"metrics": {"type": "object"}, "source": {"type": "string"}, "run_id": {"type": "string"}, "task_id": {"type": "string"}},
    "org_retrieve_memory": {"namespace": {"type": "string"}, "query": {"type": "string"}, "limit": {"type": "number"}, "requester": {"type": "string"}},
    "org_gc_memory": {},
    "org_gc_conversation": {"inactivity_days": {"type": "number"}},
    "org_gc_evidence": {},
    "org_gc_payloads": {"grace_days": {"type": "number"}, "now": {"type": "number"}},
    "org_add_evidence": {"evidence_id": {"type": "string"}, "goal_id": {"type": "string"}, "kind": {"type": "string"}, "metadata": {"type": "object"}, "owner": {"type": "string"}, "artifact_path": {"type": "string"}, "run_id": {"type": "string"}, "retention": {"type": "string"}},
    "org_get_evidence": {"evidence_id": {"type": "string"}, "goal_id": {"type": "string"}, "requester": {"type": "string"}, "deep": {"type": "boolean"}, "fields": {"type": "array"}, "byte_limit": {"type": "number"}},
    "org_set_evidence_hold": {"evidence_id": {"type": "string"}, "retention": {"type": "string"}, "actor": {"type": "string"}},
    "org_subscribe_goal": {"goal_id": {"type": "string"}, "subscriber_id": {"type": "string"}, "event_types": {"type": "array"}},
    "org_poll_goal": {"goal_id": {"type": "string"}, "subscriber_id": {"type": "string"}, "limit": {"type": "number"}, "byte_limit": {"type": "number"}, "reconnect": {"type": "boolean"}},
    "org_ack_goal": {"goal_id": {"type": "string"}, "subscriber_id": {"type": "string"}, "sequence": {"type": "number"}},
    "org_follow_up": {"goal_id": {"type": "string"}, "conversation_id": {"type": "string"}, "message": {"type": "string"}, "resume_memory": {"type": "boolean"}, "actor": {"type": "string"}},
    "org_feedback": {"goal_id": {"type": "string"}, "kind": {"type": "string", "enum": ["constraint", "question", "lesson", "redirect"]}, "content": {"type": "string"}, "actor": {"type": "string"}},
    "org_resume_goal": {"goal_id": {"type": "string"}, "actor": {"type": "string"}},
    "org_cancel_goal": {"goal_id": {"type": "string"}, "reason": {"type": "string"}, "actor": {"type": "string"}},
    "org_daemon": {"action": {"type": "string", "enum": ["start", "tick", "stop", "health", "service_plan"]}},
    "org_service_fixture": {"action": {"type": "string", "enum": ["install", "uninstall", "install-unit", "uninstall-unit"]}, "manager": {"type": "string", "enum": ["launchd", "systemd"]}, "fixture_root": {"type": "string"}, "state_path": {"type": "string"}, "command": {"type": "array"}},
    "org_service": {"action": {"type": "string", "enum": ["install", "uninstall", "status"]}, "manager": {"type": "string", "enum": ["launchd", "systemd"]}, "root": {"type": "string"}, "state_path": {"type": "string"}, "command": {"type": "array"}},
    "org_free_provider_fixture": {"provider": {"type": "string"}, "model": {"type": "string"}, "quota": {"type": "string"}, "response": {"type": "string"}, "eligibility": {"type": "string", "enum": ["eligible", "ineligible", "unknown"]}},
    "org_configure_free_routing": {"providers": {"type": "array"}, "enabled": {"type": "boolean"}, "actor": {"type": "string"}},
    "org_configure_provider_eligibility": {"provider": {"type": "string"}, "model": {"type": "string"}, "status": {"type": "string", "enum": ["eligible", "ineligible", "unknown"]}, "actor": {"type": "string"}, "source": {"type": "string"}},
    "org_interrupted_action": {"action": {"type": "string", "enum": ["record", "reconcile"]}, "goal_id": {"type": "string"}, "idempotency_key": {"type": "string"}, "phase": {"type": "string"}, "payload": {"type": "object"}},
    "org_record_quota": {"provider": {"type": "string"}, "account_ref": {"type": "string"}, "project_id": {"type": "string"}, "model": {"type": "string"}, "window": {"type": "string"}, "used": {"type": "object"}, "limits": {"type": "object"}, "reset_at": {"type": "number"}, "confidence": {"type": "string"}},
    "org_register_credential_ref": {"provider": {"type": "string"}, "account_ref": {"type": "string"}, "secret_ref": {"type": "string"}, "fingerprint": {"type": "string"}, "source": {"type": "string"}},
    "org_free_route": {"providers": {"type": "array"}, "payload": {"type": "object"}, "fallback_enabled": {"type": "boolean"}, "mode": {"type": "string", "enum": ["production", "fixture"]}},
    "org_register_model": {"profile_id": {"type": "string"}, "provider": {"type": "string"}, "runtime": {"type": "string"}, "model": {"type": "string"}, "owner": {"type": "string"}, "baseline": {"type": "object"}, "compatibility": {"type": "object"}, "routing_override": {"type": "object"}},
    "org_register_skill": {"skill_id": {"type": "string"}, "version": {"type": "number"}, "content": {"type": "string"}, "provenance": {"type": "string"}, "trust_status": {"type": "string"}, "owner": {"type": "string"}, "compatible_models": {"type": "array"}, "required_tools": {"type": "array"}, "required_docs": {"type": "array"}, "discover_on_demand": {"type": "boolean"}},
    "org_register_rule": {"rule_id": {"type": "string"}, "version": {"type": "number"}, "content": {"type": "string"}, "provenance": {"type": "string"}, "trust_status": {"type": "string"}, "owner": {"type": "string"}, "conflicts": {"type": "array"}, "precedence": {"type": "string"}},
    "org_validate_components": {"skills": {"type": "array"}, "rules": {"type": "array"}, "capabilities": {"type": "array"}, "loaded_docs": {"type": "array"}, "layers": {"type": "object"}},
}

REQUIRED_ARGS = {
    "org_create_goal": ("goal_id", "title", "owner"),
    "org_checkpoint": ("goal_id", "next_action", "recoverability", "owner"),
    "org_follow_up": ("goal_id", "conversation_id", "message"),
    "org_resume_goal": ("goal_id",),
    "org_cancel_goal": ("goal_id", "reason"),
    "org_service_fixture": ("action", "manager", "fixture_root"),
    "org_service": ("action", "manager", "root"),
    "org_register_credential_ref": ("provider", "account_ref", "secret_ref", "fingerprint"),
    "org_evaluate_budget": ("policy", "telemetry"),
    "org_record_budget_telemetry": ("metrics", "source"),
    "org_feedback": ("goal_id", "kind", "content"),
    "org_configure_free_routing": ("providers",),
    "org_configure_provider_eligibility": ("provider", "model", "status"),
}

# Managed runs are deliberately process-local: the MCP server owns the
# subprocess handle, while durable task/run state remains in SQLite.
ACTIVE_RUNS = {}
ACTIVE_QUEUE_IDS = {}
MANAGED_SCHEDULERS = {}
MANAGED_FUTURES = {}
MCP_RECOVERY_LOCK = __import__("threading").RLock()
ORGANIZATION_DAEMONS = {}

def _managed_scheduler(store: Store, capacity: int) -> ManagedScheduler:
    """Return the scheduler owned by the durable organization daemon."""
    key = str(store.path.resolve())
    daemon = ORGANIZATION_DAEMONS.get(key)
    if daemon is None:
        daemon = OrganizationDaemon(store, mode="foreground"); daemon.start(); ORGANIZATION_DAEMONS[key] = daemon
    scheduler = daemon.scheduler(capacity)
    MANAGED_SCHEDULERS[key] = scheduler
    return scheduler

def _external_publish_executor(payload):
    raise NotImplementedError("publish executor is external")

def _destructive_delete_executor(target):
    raise NotImplementedError("delete executor is external")

def _history_enabled(store: Store) -> bool:
    config = store.path.parent / "config.json"
    if not config.exists(): return True
    try: return json.loads(config.read_text()).get("control", {}).get("policy", "lock") == "lock"
    except (OSError, json.JSONDecodeError): return True


def _resolved_profile(store: Store, task_id: str, worker_id: str, dispatch: dict):
    config_path = store.path.parent / "config.json"
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    worker = next((item for item in config.get("workers", []) if item.get("id") == worker_id), {})
    task = store.task(task_id)
    task_override = json.loads(task["execution_json"]) if task and task["execution_json"] else {}
    return resolve_profile(config.get("execution", {}), worker, task_override, dispatch.get("execution", {}))


def _resolved_provider(store: Store, task_id: str, worker_id: str, dispatch: dict) -> str:
    config_path = store.path.parent / "config.json"
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    worker = next((item for item in config.get("workers", []) if item.get("id") == worker_id), {})
    task = store.task(task_id)
    # Explicit dispatch > task > worker > project/provider default.
    return (dispatch.get("provider") or (task["provider"] if task and task["provider"] else None)
            or worker.get("provider") or (config.get("providers") or ["codex"])[0])


def _history_limit(profile) -> int:
    return profile.context.history_limit

def _invalid_field(field: str, expected_schema: str, value) -> None:
    raise ValueError(json.dumps({"invalid_field": field, "expected_schema": expected_schema,
                                 "received_type": type(value).__name__}, sort_keys=True))

def _validate_start_worker_pack(args: dict) -> None:
    """Validate worker-pack shaped fields before scheduler/provider side effects."""
    for field in ("rules", "skills"):
        value = args.get(field, [])
        if not isinstance(value, list):
            _invalid_field(field, "array<object>", value)
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                _invalid_field(f"{field}[{index}]", "object", item)
    resources = args.get("resources", [])
    if not isinstance(resources, list):
        _invalid_field("resources", "array<{resource:string,mode:READ|WRITE}>", resources)
    for index, item in enumerate(resources):
        if not isinstance(item, dict):
            _invalid_field(f"resources[{index}]", "{resource:string,mode:READ|WRITE}", item)
        if not isinstance(item.get("resource"), str) or not item.get("resource"):
            _invalid_field(f"resources[{index}].resource", "non-empty string", item.get("resource"))
        if item.get("mode", "WRITE") not in {"READ", "WRITE"}:
            _invalid_field(f"resources[{index}].mode", "READ|WRITE", item.get("mode"))
    execution = args.get("execution", {}) or {}
    if not isinstance(execution, dict):
        _invalid_field("execution", "object", execution)
    mode = execution.get("mode", ExecutionMode.ISOLATED_SANDBOX.value)
    if mode not in {item.value for item in ExecutionMode}:
        _invalid_field("execution.mode", "|".join(item.value for item in ExecutionMode), mode)

def _authority(args: dict) -> AuthorityPolicy:
    execution = args.get("execution", {}) or {}
    mode = ExecutionMode(execution.get("mode", ExecutionMode.ISOLATED_SANDBOX.value))
    capabilities = frozenset(execution.get("capabilities", ["filesystem.read", "provider.preflight"]))
    return AuthorityPolicy(mode, capabilities)

def _persisted_mutation_authority(store: Store, args: dict) -> AuthorityPolicy:
    """Resolve mutation authority from the exact durable run/task scope.

    Request execution capabilities can narrow the persisted set, never expand it.
    """
    run_id, task_id = args.get("run_id"), args.get("task_id")
    if not run_id or not task_id:
        raise PermissionError("mutation requires exact run_id and task_id")
    run = store.db.execute("SELECT task_id FROM runs WHERE id=?", (run_id,)).fetchone()
    if not run or run["task_id"] != task_id:
        raise PermissionError("run/task scope mismatch")
    row = store.authority_snapshot(run_id)
    if not row: raise PermissionError("persisted run authority required")
    persisted = frozenset(json.loads(row["capabilities"]))
    requested = args.get("execution", {}).get("capabilities")
    if requested is not None and not set(requested).issubset(persisted):
        raise PermissionError("requested capabilities exceed persisted run authority")
    caps = persisted if requested is None else frozenset(requested)
    return AuthorityPolicy(ExecutionMode(row["mode"]), caps)

def _context(args: dict):
    value = (args.get("context") or {}).copy()
    if not value: return None
    if isinstance(value.get("runtime"), dict):
        from .authority import RuntimeReport
        value["runtime"] = RuntimeReport(**value["runtime"])
    for key in ("authorizations", "prohibited_actions", "resources", "leases", "relevant_files", "acceptance_criteria", "stop_conditions"):
        value[key] = tuple(value.get(key, ()))
    return ContextPacket(**value)

def _context_from_snapshot(value):
    if not value: return None
    value = dict(value)
    if isinstance(value.get("runtime"), dict):
        from .authority import RuntimeReport
        value["runtime"] = RuntimeReport(**value["runtime"])
    for key in ("authorizations", "prohibited_actions", "resources", "leases", "relevant_files", "acceptance_criteria", "stop_conditions"):
        value[key] = tuple(value.get(key, ()))
    return ContextPacket(**value)

def _recover_live_job(store: Store, queue_id: int, job: ManagedJob) -> None:
    key = (job.task_id, job.worker_id)
    adapter = job.adapter
    if job.resume_session_id:
        run = resume_managed_worker(store, job.task_id, job.worker_id, adapter, job.resume_session_id,
                                     job.prompt, job.cwd, profile=job.profile,
                                     resume_from_task_id=job.resume_from_task_id,
                                     authority=job.authority, context=job.context,
                                     context_checkpoint=job.context_checkpoint)
    else:
        run = start_managed_worker(store, job.task_id, job.worker_id, adapter, job.prompt, job.cwd,
                                    profile=job.profile, authority=job.authority, context=job.context)
    store.mark_managed_started(queue_id)
    ACTIVE_RUNS[key] = run
    ACTIVE_QUEUE_IDS[key] = queue_id


def dispatch(store: Store, method: str, args: dict, _daemon_owner: bool = False, _trusted_test: bool = False) -> object:
    if not _daemon_owner:
        marker = store.path.parent / "daemon.ipc.json"
        if marker.exists():
            try:
                socket_path = json.loads(marker.read_text())["socket"]
                from .daemon_ipc import DaemonIPCClient
                response = DaemonIPCClient(socket_path).call({"method": method, "params": args})
                if response.get("ok"):
                    return response["result"]
                raise RuntimeError(response.get("error", "daemon IPC request failed"))
            except (OSError, ValueError, KeyError, ConnectionError):
                # A stale marker is fail-closed for managed execution but does
                # not prevent local ping/diagnostic calls from functioning.
                if method not in {"ping", "initialize"}:
                    raise RuntimeError("daemon IPC owner is unavailable")
    if method == "ping":
        return {}
    if method == "initialize":
        recovered = store.reconcile(HOST_INSTANCE_ID)
        config_path = store.path.parent / "config.json"
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
        scheduler = _managed_scheduler(store, int(config.get("execution", {}).get("max_concurrent_workers", 2)))
        recovered_count = len(recovered) + scheduler.recover_queued(
            provider, profile_from,
            lambda value: AuthorityPolicy(ExecutionMode(value.get("mode", "isolated_sandbox")), frozenset(value.get("capabilities", []))),
            _context_from_snapshot, lambda queue_id, job: _recover_live_job(store, queue_id, job))
        return {"protocolVersion": "2024-11-05", "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "agent-control-plane", "version": "0.5.2"},
                "instructions": f"Startup reconciliation recovered {recovered_count} run(s). You are the Master supervisor. Use MAC Control to register your master_id and request a complete worker group before dispatch. In lock mode, worker capacity is fixed and shortages return PENDING/rejected; flexible mode permits temporary worker counts but does not persist chat history. In lock mode preserve master_id and conversation_id for history; do not silently continue a changed conversation. Use isolated worktrees, never merge worker changes, validate before acceptance, and report status, validation, commit, blockers, and conflicts."}
    if method == "tools/list":
        return {"tools": [{"name": name, "description": f"control-plane {name}",
                            "inputSchema": {"type": "object", "properties": props, **({"required": list(REQUIRED_ARGS[name])} if name in REQUIRED_ARGS else {})}}
                         for name, props in TOOL_DEFS.items()]}
    if method != "tools/call":
        raise ValueError(f"unsupported method: {method}")
    name, a = args.get("name"), args.get("arguments", {})
    missing = [key for key in REQUIRED_ARGS.get(name, ()) if key not in a]
    if missing:
        raise ValueError(f"missing required MCP arguments: {','.join(missing)}")
    if name == "status":
        if a.get("inspection_mode") == "result_only":
            if not a.get("task_id"):
                _invalid_field("task_id", "non-empty string required for result_only", a.get("task_id"))
            payload = store.result_only_snapshot(a["task_id"], int(a.get("byte_limit", 8192)))
        elif a.get("inspection_mode", "full") == "full":
            payload = store.snapshot()
        else:
            _invalid_field("inspection_mode", "result_only|full", a.get("inspection_mode"))
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if a.get("inspection_mode") == "result_only" else json.dumps(payload, ensure_ascii=False)
        return {"content": [{"type": "text", "text": text}]}
    if name == "org_create_goal": return {"content": [{"type": "text", "text": json.dumps(org_create_goal(store, a["goal_id"], a["title"], a["owner"], a.get("specialist_id"), a.get("acceptance_criteria"), a.get("inspection_mode", "result_only"), a.get("checkpoint_policy"), a.get("worker_profile"), a.get("worker_pack"), a.get("permissions"), a.get("worker_class", "basic")))}]}
    if name == "org_goal_summary": return {"content": [{"type": "text", "text": json.dumps(goal_summary(store, a["goal_id"], int(a.get("cursor", 0)), int(a.get("limit", 20)), a.get("fields")))}]}
    if name == "org_goal_event": return {"content": [{"type": "text", "text": json.dumps(append_goal_event(store, a["goal_id"], a["kind"], a.get("payload", {}), a["actor"]))}]}
    if name == "org_checkpoint": return {"content": [{"type": "text", "text": json.dumps(goal_checkpoint(store, a["goal_id"], a.get("state", {}), a.get("evidence", []), a["next_action"], a["recoverability"], a["owner"]))}]}
    if name == "org_set_inspection": set_inspection_mode(store, a["goal_id"], a["mode"], a["actor"]); return {"content": [{"type": "text", "text": "inspection mode updated"}]}
    if name == "org_propose_memory": return {"content": [{"type": "text", "text": json.dumps(propose_memory(store, a["goal_id"], a["content"], a["scope"], a["source"], a["owner"], a.get("metadata")))}]}
    if name == "org_promote_memory": return {"content": [{"type": "text", "text": json.dumps(promote_memory(store, a["memory_id"], a["scope"], a["actor"]))}]}
    if name == "org_create_archetype": return {"content": [{"type": "text", "text": json.dumps(create_archetype(store, a["archetype_id"], int(a["version"]), a["name"], a["role"], a["owner"], taxonomy=a.get("taxonomy", []), skills=a.get("skills", []), rules=a.get("rules", [])))}]}
    if name == "org_assign_archetype": return {"content": [{"type": "text", "text": json.dumps(assign_archetype(store, a["goal_id"], a["instance_id"], a["archetype_id"], int(a["version"]), a["actor"]))}]}
    if name == "org_inspect_archetype": return {"content": [{"type": "text", "text": json.dumps(inspect_archetype(store, a["archetype_id"], a.get("version")))}]}
    if name == "org_set_archetype_enabled": return {"content": [{"type": "text", "text": json.dumps(set_archetype_enabled(store, a["archetype_id"], int(a["version"]), bool(a["enabled"]), a["actor"]))}]}
    if name == "org_observe": return {"content": [{"type": "text", "text": json.dumps({"observation_id": record_observation(store, a["observation"])})}]}
    if name == "org_recommend_model": return {"content": [{"type": "text", "text": json.dumps(recommend_model(store, a["taxonomy"], int(a.get("minimum_samples", 3)), a.get("dimensions")))}]}
    if name == "org_recommend_worker": return {"content": [{"type": "text", "text": json.dumps(recommend_worker(store, a["goal"], a.get("risk", "standard"), a.get("constraints")))}]}
    if name == "org_compile_pack": return {"content": [{"type": "text", "text": json.dumps(compile_worker_pack(store, a["pack_id"], a["goal_id"], a["owner"], a["components"]))}]}
    if name == "org_evaluate_budget": return {"content": [{"type": "text", "text": json.dumps(evaluate_budget(a.get("policy"), a.get("telemetry")))}]}
    if name == "org_record_budget_telemetry": return {"content": [{"type": "text", "text": json.dumps(record_budget_telemetry(store, a["metrics"], a["source"], a.get("run_id"), a.get("task_id")))}]}
    if name == "org_retrieve_memory": return {"content": [{"type": "text", "text": json.dumps(retrieve_memory(store, a["namespace"], a["query"], int(a.get("limit", 10)), a.get("requester", "master")))}]}
    if name == "org_gc_memory": return {"content": [{"type": "text", "text": json.dumps({"expired": garbage_collect_memory(store)})}]}
    if name == "org_gc_conversation": return {"content": [{"type": "text", "text": json.dumps({"expired": garbage_collect_conversation(store, inactivity_days=int(a.get("inactivity_days", 30)))})}]}
    if name == "org_gc_evidence": return {"content": [{"type": "text", "text": json.dumps({"tombstoned": garbage_collect_evidence(store, a.get("now"), int(a.get("inactivity_days", 30)))})}]}
    if name == "org_gc_payloads": return {"content": [{"type": "text", "text": json.dumps(physically_collect_payloads(store, a.get("now"), int(a.get("grace_days", 7))))}]}
    if name == "org_add_evidence": return {"content": [{"type": "text", "text": json.dumps(add_evidence(store, a["evidence_id"], a["goal_id"], a["kind"], a.get("metadata", {}), a["owner"], a.get("artifact_path"), a.get("run_id"), a.get("retention", "transient")))}]}
    if name == "org_get_evidence": return {"content": [{"type": "text", "text": json.dumps(get_evidence(store, a["evidence_id"], a["goal_id"], a["requester"], bool(a.get("deep", False)), a.get("fields"), int(a.get("byte_limit", 16384))))}]}
    if name == "org_set_evidence_hold": set_evidence_hold(store, a["evidence_id"], a["retention"], a["actor"]); return {"content": [{"type": "text", "text": "evidence retention updated"}]}
    if name == "org_subscribe_goal": return {"content": [{"type": "text", "text": json.dumps(subscribe_goal(store, a["goal_id"], a["subscriber_id"], a.get("event_types")))}]}
    if name == "org_poll_goal": return {"content": [{"type": "text", "text": json.dumps(poll_goal(store, a["goal_id"], a["subscriber_id"], int(a.get("limit", 20)), int(a.get("byte_limit", 16384)), bool(a.get("reconnect", False))))}]}
    if name == "org_ack_goal": return {"content": [{"type": "text", "text": json.dumps(acknowledge_goal(store, a["goal_id"], a["subscriber_id"], int(a["sequence"]))) }]}
    if name == "org_follow_up": return {"content": [{"type": "text", "text": json.dumps(follow_up(store, a["goal_id"], a["conversation_id"], a["message"], bool(a.get("resume_memory", True)), a.get("actor", "master")))}]}
    if name == "org_feedback": return {"content": [{"type": "text", "text": json.dumps(record_feedback(store, a["goal_id"], a["kind"], a["content"], a.get("actor", "master")))}]}
    if name == "org_resume_goal": return {"content": [{"type": "text", "text": json.dumps(resume_goal(store, a["goal_id"], a.get("actor", "master")))}]}
    if name == "org_cancel_goal": return {"content": [{"type": "text", "text": json.dumps(cancel_goal(store, a["goal_id"], a["reason"], a.get("actor", "master")))}]}
    if name == "org_daemon":
        daemon = OrganizationDaemon(store)
        action = a.get("action", "health")
        result = daemon.start() if action == "start" else daemon.tick() if action == "tick" else daemon.stop() if action == "stop" else daemon.service_plan() if action == "service_plan" else daemon.health()
        return {"content": [{"type": "text", "text": json.dumps(result)}]}
    if name == "org_service_fixture":
        manager = SandboxedServiceManager(a["fixture_root"], a["manager"])
        if a["action"] == "install":
            result = manager.install(a.get("command", []), a.get("state_path", str(store.path)))
        elif a["action"] == "install-unit":
            result = manager.install_unit(a.get("command", []), a.get("state_path", str(store.path)))
        elif a["action"] == "uninstall":
            result = {"removed": manager.uninstall()}
        elif a["action"] == "uninstall-unit":
            result = {"removed": manager.uninstall_unit()}
        else:
            raise ValueError("unsupported service fixture action")
        return {"content": [{"type": "text", "text": json.dumps(result)}]}
    if name == "org_service":
        manager = ProductionServiceManager(a["root"], a["manager"], subprocess_service_executor(a["manager"]))
        action = a["action"]
        result = manager.install(a.get("command", ["acp", "daemon-ipc"]), a.get("state_path", str(store.path))) if action == "install" else manager.uninstall() if action == "uninstall" else manager.status()
        return {"content": [{"type": "text", "text": json.dumps(result)}]}
    if name == "org_register_credential_ref":
        return {"content": [{"type": "text", "text": json.dumps(register_credential_ref(store, a["provider"], a["account_ref"], a["secret_ref"], a["fingerprint"], a.get("source", "environment")))}]}
    if name == "org_free_provider_fixture":
        result, output = RecordedFreeProvider(a["provider"], a.get("model", "free-fixture"), a.get("response", "ok"), a.get("eligibility", "unknown")).request(quota=a.get("quota", "available"))
        return {"content": [{"type": "text", "text": json.dumps({"quota": result.__dict__, "output": output})}]}
    if name == "org_interrupted_action":
        daemon = OrganizationDaemon(store)
        result = daemon.record_interrupted_action(a["goal_id"], a["idempotency_key"], a["phase"], a.get("payload", {})) if a.get("action") == "record" else daemon.reconcile_interrupted(a["goal_id"])
        return {"content": [{"type": "text", "text": json.dumps(result)}]}
    if name == "org_record_quota": return {"content": [{"type": "text", "text": json.dumps(record_quota(store, a["provider"], a["account_ref"], a["model"], a["window"], a.get("used", {}), a.get("limits", {}), a.get("reset_at"), a.get("confidence", "unknown"), a.get("project_id", "")))}]}
    if name == "org_free_route":
        providers = a.get("providers", [])
        dangerous = {"secret", "http_response", "transport"}
        if any(dangerous.intersection(item) for item in providers):
            raise ValueError("raw secret/http_response/transport is forbidden on MCP")
        mode = a.get("mode", "production")
        if mode not in {"production", "fixture"}:
            raise ValueError("invalid provider route mode")
        if mode == "fixture" and not _trusted_test:
            raise PermissionError("fixture mode is available only through trusted test dispatch")
        fixture_fields = {"quota", "eligibility", "response", "usage"}
        if mode == "production" and any(fixture_fields.intersection(item) for item in providers):
            raise ValueError("fixture provider fields require explicit trusted fixture mode")
        return {"content": [{"type": "text", "text": json.dumps(free_route(store, providers, a.get("payload", {}), fallback_enabled=bool(a.get("fallback_enabled", False)), production=mode == "production"))}]}
    if name == "org_configure_free_routing": return {"content": [{"type": "text", "text": json.dumps(configure_free_routing(store, a["providers"], a.get("actor", "master"), bool(a.get("enabled", True))))}]}
    if name == "org_configure_provider_eligibility": return {"content": [{"type": "text", "text": json.dumps(configure_provider_eligibility(store, a["provider"], a["model"], a["status"], a.get("actor", "master"), a.get("source", "recorded")))}]}
    if name == "org_register_model": return {"content": [{"type": "text", "text": json.dumps(register_model_profile(store, a["profile_id"], a["provider"], a["runtime"], a["model"], a["owner"], a.get("baseline", {}), a.get("compatibility", {}), a.get("routing_override")))}]}
    if name == "org_register_skill": return {"content": [{"type": "text", "text": json.dumps(register_skill(store, a["skill_id"], int(a["version"]), a["content"], a["provenance"], a["trust_status"], a["owner"], a.get("compatible_models"), a.get("required_tools"), a.get("required_docs"), bool(a.get("discover_on_demand", False))))}]}
    if name == "org_register_rule": return {"content": [{"type": "text", "text": json.dumps(register_rule(store, a["rule_id"], int(a["version"]), a["content"], a["provenance"], a["trust_status"], a["owner"], a.get("conflicts"), a.get("precedence", "project")))}]}
    if name == "org_validate_components": return {"content": [{"type": "text", "text": json.dumps(validate_worker_components(store, [tuple(x) for x in a.get("skills", [])], [tuple(x) for x in a.get("rules", [])], set(a.get("capabilities", [])), set(a.get("loaded_docs", [])), a.get("layers")))}]}
    if name == "managed_queue_status": return {"content": [{"type": "text", "text": json.dumps(store.managed_queue())}]}
    if name == "create_approval":
        payload = a["payload"]
        store.create_approval(ApprovalToken(a["token_id"], a["capability"], a["project_id"], a["provider_id"], a["job_id"], digest(payload), a["idempotency_key"], int(a.get("max_attempts", 1)), expires_at=float(a.get("expires_at", 0)), run_id=a["run_id"], task_id=a["task_id"]))
        return {"content": [{"type": "text", "text": "approval created"}]}
    if name == "consume_approval":
        policy = AuthorityPolicy(capabilities=frozenset({a["capability"]}))
        store.consume_approval(a["token_id"], policy, a["payload"], a["project_id"], a["provider_id"], a["job_id"], a["idempotency_key"], a["run_id"], a["task_id"])
        return {"content": [{"type": "text", "text": "approval consumed"}]}
    if name == "authorize_action":
        authorize_action(store, _persisted_mutation_authority(store, a), a["capability"], a.get("payload", {}), a.get("token_id"), a.get("destructive", False), a.get("external", False), a.get("project_id"), a.get("provider_id"), a.get("job_id"), a.get("idempotency_key"), a.get("run_id"), a.get("task_id"))
        return {"content": [{"type": "text", "text": "authorized"}]}
    if name == "authorize_runtime_action":
        from .authority import RuntimeReport
        authorize_runtime_action(store, _persisted_mutation_authority(store, a), a["capability"], RuntimeReport(**a.get("report", {})), a.get("resource"), a.get("task_id"), a.get("worker_id"), a.get("active_job", False), a.get("target_id"), a.get("project_id"), a.get("operation"))
        return {"content": [{"type": "text", "text": "runtime action authorized"}]}
    if name == "report_blocker":
        stopped = report_blocker(store, a["task_id"], a["blocker"], a["external_state_version"], int(a.get("threshold", 3)))
        return {"content": [{"type": "text", "text": json.dumps({"stopped": stopped})}]}
    if name == "review_evidence":
        return {"content": [{"type": "text", "text": json.dumps(store.review_evidence(a["task_id"]))}]}
    if name == "control_status":
        mode = a.get("inspection_mode", "full")
        if mode == "result_only":
            if not a.get("task_id"):
                _invalid_field("task_id", "non-empty string required for result_only", a.get("task_id"))
            payload = store.result_only_snapshot(a["task_id"], int(a.get("byte_limit", 8192)))
        elif mode == "full":
            payload = store.control_snapshot()
        else:
            _invalid_field("inspection_mode", "result_only|full", mode)
        return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}]}
    if name == "control_set_policy":
        store.control_set_policy(a["policy"]); return {"content": [{"type": "text", "text": "control policy updated"}]}
    if name == "control_register_master":
        store.control_register_boss(a["master_id"], a.get("name"), int(a.get("min_workers", 1)), int(a.get("max_workers", a.get("min_workers", 1))))
        return {"content": [{"type": "text", "text": "master registered"}]}
    if name == "control_request_master_workers":
        return {"content": [{"type": "text", "text": json.dumps(store.control_request_workers(a["master_id"], a.get("requested")))}]}
    if name == "control_release_master":
        return {"content": [{"type": "text", "text": json.dumps(store.control_release_boss(a["master_id"]))}]}
    if name == "control_register_boss":
        store.control_register_boss(a["boss_id"], a.get("name"), int(a.get("min_workers", 1)), int(a.get("max_workers", a.get("min_workers", 1))))
        return {"content": [{"type": "text", "text": "boss registered"}]}
    if name == "control_request_workers":
        return {"content": [{"type": "text", "text": json.dumps(store.control_request_workers(a["boss_id"], a.get("requested")))}]}
    if name == "control_release_boss":
        return {"content": [{"type": "text", "text": json.dumps(store.control_release_boss(a["boss_id"]))}]}
    if name == "history_add":
        store.record_history(a["conversation_id"], a["role"], a["actor"], a["content"], a.get("task_id"), a.get("worker_id"))
        return {"content": [{"type": "text", "text": "recorded"}]}
    if name == "history_list":
        return {"content": [{"type": "text", "text": json.dumps([dict(row) for row in store.history(a["conversation_id"], a.get("limit", 50))], ensure_ascii=False)}]}
    if name == "knowledge_load":
        return {"content": [{"type": "text", "text": json.dumps({"digest": store.load_knowledge(a["path"])})}]}
    if name == "knowledge_ack":
        store.acknowledge_knowledge(a["actor"], a.get("worker_id")); return {"content": [{"type": "text", "text": "acknowledged"}]}
    if name == "run_worker":
        prompt = a["prompt"]
        profile = _resolved_profile(store, a["task_id"], a["worker_id"], a)
        if a.get("conversation_id") and _history_enabled(store):
            context = list(reversed(store.history(a["conversation_id"], _history_limit(profile))))
            prompt += "\n\nSHARED BOSS/WORKER HISTORY:\n" + "\n".join(f"[{row['role']}/{row['actor']}] {row['content']}" for row in context)
        config_path = store.path.parent / "config.json"
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
        scheduler = _managed_scheduler(store, int(config.get("execution", {}).get("max_concurrent_workers", 2)))
        job = ManagedJob(a["task_id"], a["worker_id"], provider(_resolved_provider(store, a["task_id"], a["worker_id"], a)), prompt, a["cwd"],
                                                   profile=profile, authority=_authority(a), context=_context(a),
                                                   conversation_id=a.get("conversation_id"), resume_session_id=a.get("resume_session_id"),
                                                   resume_from_task_id=a.get("resume_from_task_id"), context_checkpoint=a.get("context_checkpoint"),
                                                   preserve_resume_snapshot=bool(a.get("resume_session_id") and not a.get("execution")))
        submitted = scheduler.submit(job)
        if submitted["status"] != "STARTED":
            return {"content": [{"type": "text", "text": json.dumps({"status": submitted["status"], "queue_id": submitted["queue_id"], "reason": submitted["reason"]})}]}
        key = (a["task_id"], a["worker_id"])
        MANAGED_FUTURES[key] = (scheduler, submitted["future"], submitted["queue_id"])
        if a.get("wait", False):
            result = submitted["future"].result()
            MANAGED_FUTURES.pop(key, None)
            return {"content": [{"type": "text", "text": json.dumps({"exit_code": result.exit_code, "session_id": result.session_id, "status": store.task(a["task_id"])["status"]})}]}
        return {"content": [{"type": "text", "text": json.dumps({"status": "RUNNING", "queue_id": submitted["queue_id"]})}]}
    if name == "start_worker":
        _validate_start_worker_pack(a)
        key = (a["task_id"], a["worker_id"])
        # ACTIVE_RUNS is process-local; temporary/test state databases may
        # reuse task IDs, so discard a handle that is not present in this DB.
        if key in ACTIVE_RUNS and store.latest_run(a["task_id"], a["worker_id"]) is None:
            stale = ACTIVE_RUNS.pop(key, None)
            process = getattr(stale, "process", None)
            if process is not None and process.poll() is None:
                process.terminate()
            stdout = getattr(process, "stdout", None)
            if stdout is not None:
                stdout.close()
            ACTIVE_QUEUE_IDS.pop(key, None)
        if key in ACTIVE_RUNS:
            raise ValueError("managed worker is already active")
        config_path = store.path.parent / "config.json"
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
        capacity = int(config.get("execution", {}).get("max_concurrent_workers", 2))
        provider_name = _resolved_provider(store, a["task_id"], a["worker_id"], a)
        task = store.task(a["task_id"])
        if task is None:
            raise ValueError(f"unknown task: {a['task_id']}")
        # Dependency readiness is a dispatch gate, not a queue side effect.
        _dependency_preflight(store, task, a["task_id"], a["worker_id"])
        prompt = a["prompt"]
        profile = _resolved_profile(store, a["task_id"], a["worker_id"], a)
        if a.get("conversation_id") and _history_enabled(store):
            context = list(reversed(store.history(a["conversation_id"], _history_limit(profile))))
            prompt += "\n\nSHARED BOSS/WORKER HISTORY:\n" + "\n".join(f"[{row['role']}/{row['actor']}] {row['content']}" for row in context)
        adapter = provider(provider_name)
        queued_job = ManagedJob(a["task_id"], a["worker_id"], adapter, prompt, a["cwd"],
                                 tuple((item["resource"], item.get("mode", "WRITE")) for item in a.get("resources", [])),
                                 profile=profile, authority=_authority(a), context=_context(a),
                                 conversation_id=a.get("conversation_id"), resume_session_id=a.get("resume_session_id"),
                                 resume_from_task_id=a.get("resume_from_task_id"), context_checkpoint=a.get("context_checkpoint"),
                                 lifecycle="live")
        queued_payload = ManagedScheduler._payload(queued_job)
        requested_resources = tuple((item["resource"], item.get("mode", "WRITE")) for item in a.get("resources", []))
        for resource, mode in requested_resources:
            if not store.acquire(resource, a["task_id"], a["worker_id"], mode):
                queue_id = store.enqueue_managed(a["task_id"], a["worker_id"], provider_name, f"lease unavailable: {resource}", queued_payload)
                store.set_task_status(a["task_id"], "WAITING_RESOURCE")
                ACTIVE_QUEUE_IDS[key] = queue_id
                return {"content": [{"type": "text", "text": json.dumps({"status": "QUEUED", "queue_id": queue_id, "reason": f"lease unavailable: {resource}"})}]}
        if key in ACTIVE_QUEUE_IDS:
            store.mark_managed_finished(ACTIVE_QUEUE_IDS.pop(key), "DISPATCHED")
        queue_id, admission = store.admit_managed(a["task_id"], a["worker_id"], provider_name, capacity)
        if admission != "scheduled":
            store.release_task_leases(a["task_id"])
            store.set_managed_payload(queue_id, queued_payload)
            store.set_task_status(a["task_id"], "WAITING_RESOURCE")
            ACTIVE_QUEUE_IDS[key] = queue_id
            return {"content": [{"type": "text", "text": json.dumps({"status": "QUEUED", "queue_id": queue_id, "reason": "global capacity"})}]}
        try:
            if a.get("resume_session_id"):
                if profile.context.allowed_paths:
                    raw_context, _ = _restricted_context(Path(a["cwd"]), profile.context.allowed_paths)
                else:
                    raw_context = store.knowledge_context(profile.context.knowledge_mode)
                prompt = _inject_context(prompt, raw_context, profile)
                run = resume_managed_worker(store, a["task_id"], a["worker_id"], adapter, a["resume_session_id"], prompt, a["cwd"], profile=profile, resume_from_task_id=a.get("resume_from_task_id"), authority=_authority(a), context=_context(a), context_checkpoint=a.get("context_checkpoint"))
            else:
                run = start_managed_worker(store, a["task_id"], a["worker_id"], adapter, prompt, a["cwd"], profile=profile, authority=_authority(a), context=_context(a))
        except Exception:
            store.mark_managed_finished(queue_id, "FAILED")
            raise
        ACTIVE_RUNS[key] = run
        ACTIVE_QUEUE_IDS[key] = queue_id
        return {"content": [{"type": "text", "text": json.dumps({"session_id": run.session_id, "queue_id": queue_id, "status": "RUNNING"})}]}
    if name == "pause_worker":
        key = (a["task_id"], a["worker_id"])
        run = ACTIVE_RUNS.get(key)
        if run is None: raise ValueError("managed worker is not active")
        pause_managed_worker(store, *key, run)
        return {"content": [{"type": "text", "text": "paused"}]}
    if name == "resume_worker":
        key = (a["task_id"], a["worker_id"])
        run = ACTIVE_RUNS.get(key)
        if run is None: raise ValueError("managed worker is not active")
        resume_live_worker(store, *key, run)
        return {"content": [{"type": "text", "text": "resumed"}]}
    if name == "wait_worker":
        key = (a["task_id"], a["worker_id"])
        if key in MANAGED_FUTURES:
            scheduler, future, queue_id = MANAGED_FUTURES.pop(key)
            result = future.result()
            compact = store.result_only_snapshot(a["task_id"])
            return {"content": [{"type": "text", "text": json.dumps({"exit_code": result.exit_code, "session_id": result.session_id, "status": store.task(a["task_id"])["status"], "result": compact}, ensure_ascii=False)}]}
        run = ACTIVE_RUNS.pop(key, None)
        if run is None:
            task = store.task(a["task_id"])
            latest = store.latest_run(a["task_id"], a["worker_id"])
            if task is not None and latest is not None and task["status"] in {"WAITING_DECISION", "REVIEW", "FAILED", "ACCEPTED", "DONE"}:
                compact = store.result_only_snapshot(a["task_id"])
                return {"content": [{"type": "text", "text": json.dumps({"exit_code": latest["exit_code"], "session_id": latest["session_id"], "status": task["status"], "result": compact, "replayed": True}, ensure_ascii=False)}]}
            raise ValueError("managed worker is not active")
        result = finish_managed_worker(store, *key, run)
        if key in ACTIVE_QUEUE_IDS: store.mark_managed_finished(ACTIVE_QUEUE_IDS.pop(key))
        config_path = store.path.parent / "config.json"
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
        with MCP_RECOVERY_LOCK:
            _managed_scheduler(store, int(config.get("execution", {}).get("max_concurrent_workers", 2))).recover_queued(
                provider, profile_from,
                lambda value: AuthorityPolicy(ExecutionMode(value.get("mode", "isolated_sandbox")), frozenset(value.get("capabilities", []))),
                _context_from_snapshot, lambda queue_id, job: _recover_live_job(store, queue_id, job))
        compact = store.result_only_snapshot(a["task_id"])
        return {"content": [{"type": "text", "text": json.dumps({"exit_code": result.exit_code, "session_id": result.session_id, "status": store.task(a["task_id"])["status"], "result": compact}, ensure_ascii=False)}]}
    if name == "cancel_worker":
        key = (a["task_id"], a["worker_id"])
        run = ACTIVE_RUNS.pop(key, None)
        if run is None: raise ValueError("managed worker is not active")
        cancel_managed_worker(store, *key, run, a.get("reason", "supervisor cancellation"))
        if key in ACTIVE_QUEUE_IDS: store.mark_managed_finished(ACTIVE_QUEUE_IDS.pop(key), "CANCELLED")
        return {"content": [{"type": "text", "text": "cancelled"}]}
    if name == "validate":
        exit_code = validate(store, a["task_id"], a["command"], a["cwd"], a.get("timeout", 300), _authority(a))
        return {"content": [{"type": "text", "text": json.dumps({"exit_code": exit_code})}]}
    if name == "run_activity":
        return {"content": [{"type": "text", "text": json.dumps(store.activity_snapshot(a["run_id"]), default=str)}]}
    if name == "heartbeat":
        store.heartbeat(a["run_id"], a.get("action")); return {"content": [{"type": "text", "text": "heartbeat recorded"}]}
    if name == "queue_steering":
        store.queue_steering(a["run_id"], a["instruction"], a["scope"]); return {"content": [{"type": "text", "text": "queued steering recorded"}]}
    if name == "refresh_authority":
        execution = a.get("execution", {}); policy = AuthorityPolicy(ExecutionMode(execution.get("mode", "isolated_sandbox")), frozenset(execution.get("capabilities", ["filesystem.read", "provider.preflight"])))
        packet = ContextPacket(**a["context"]); store.refresh_run_authority(a["run_id"], policy, packet)
        return {"content": [{"type": "text", "text": "authority refreshed"}]}
    if name == "dependency_check":
        result = verify_offline(DependencyContract(**a["contract"]), a["store"])
        if not result["ready"] and a.get("task_id"):
            store.set_task_status(a["task_id"], "WAITING_DEPENDENCY")
            store.add_message("BLOCKER", result, task_id=a["task_id"])
        return {"content": [{"type": "text", "text": json.dumps(result)}]}
    if name == "reconcile": return {"content": [{"type": "text", "text": json.dumps({"recovered_tasks": store.reconcile(HOST_INSTANCE_ID)})}]}
    if name == "declare_resource":
        store.add_resource(a["name"], a["kind"], a.get("paths", [])); return {"content": [{"type": "text", "text": f"created {a['name']}"}]}
    if name == "acquire_resource":
        acquired = store.acquire(a["resource"], a["task_id"], a["worker_id"], a["mode"], a.get("ttl", 300))
        return {"content": [{"type": "text", "text": json.dumps({"acquired": acquired})}]}
    if name == "renew_resource":
        renewed = store.renew(a["resource"], a["task_id"], a["worker_id"], a.get("ttl", 300))
        return {"content": [{"type": "text", "text": json.dumps({"renewed": renewed})}]}
    if name == "bump_resource":
        version = store.bump_resource(a["resource"]); return {"content": [{"type": "text", "text": json.dumps({"version": version})}]}
    if name == "create_task":
        store.add_task(a["id"], a["title"], a.get("provider"), a.get("depends_on", []), a.get("execution", {}), a.get("goal_id")); return {"content": [{"type": "text", "text": f"created {a['id']}"}]}
    if name == "create_resource":
        store.add_resource(a["name"], a["kind"], a.get("paths", [])); return {"content": [{"type": "text", "text": f"created {a['name']}"}]}
    if name == "send_message":
        message_type = a["type"]
        task_id = a.get("task_id")
        if message_type in {"QUESTION", "DECISION_REQUIRED"}:
            if not task_id:
                _invalid_field("task_id", "non-empty string required for decision messages", task_id)
            task = store.task(task_id)
            if task is None:
                raise ValueError(f"unknown task: {task_id}")
            if task["status"] == "RUNNING":
                store.set_task_status(task_id, "WAITING_DECISION")
        store.add_message(message_type, a.get("payload", {}), task_id, a.get("worker_id")); return {"content": [{"type": "text", "text": "queued"}]}
    if name == "accept_task": store.accept_task(a["task_id"]); return {"content": [{"type": "text", "text": f"accepted {a['task_id']}"}]}
    if name == "accept_integration":
        project = store.path.parent.parent
        supervisor = Supervisor(project, store=store)
        try:
            commit = supervisor.accept(a["task_id"], a["worktree"], a["commit_message"], tuple(a.get("validation", [])))
        finally:
            # The MCP server owns the store connection and closes it at shutdown.
            pass
        return {"content": [{"type": "text", "text": json.dumps({"commit": commit, "status": store.task(a["task_id"])["status"]})}]}
    if name == "push_integration":
        policy = _persisted_mutation_authority(store, a)
        supervisor = Supervisor(store.path.parent.parent, store=store)
        pushed = supervisor.push(a["remote"], a["branch"], policy, a["token_id"], a["project_id"], a["provider_id"], a["job_id"], a["idempotency_key"], a["run_id"], a["task_id"])
        return {"content": [{"type": "text", "text": json.dumps({"result": pushed})}]}
    if name == "provider_submit":
        approval = a.get("approval", {}); scoped = {**a, **approval}
        policy = _persisted_mutation_authority(store, scoped)
        result = submit_provider(store, policy, a["provider"], a["job_id"], a["payload"], provider(a["provider"]).submit,
                                 paid=a.get("paid", False), approval_scope=approval)
        return {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}
    if name == "external_publish":
        approval = a.get("approval", {}); scoped = {**a, **approval}
        policy = _persisted_mutation_authority(store, scoped)
        result = publish_external(store, policy, a["payload"], _external_publish_executor, **approval)
        return {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}
    if name == "destructive_delete":
        approval = a.get("approval", {}); scoped = {**a, **approval}
        policy = _persisted_mutation_authority(store, scoped)
        result = delete_destructive(store, policy, a["target"], _destructive_delete_executor, **approval)
        return {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}
    if name == "arbitrate_conflict":
        result = arbitrate_conflict(store, a["task_id"], a["decision_id"], provider(a.get("provider", "codex")), a["evidence"], a["cwd"])
        return {"content": [{"type": "text", "text": json.dumps({"exit_code": result.exit_code, "status": store.task(a["task_id"])["status"]})}]}
    if name == "pause_task": store.pause_task(a["task_id"], a.get("reason", "supervisor pause")); return {"content": [{"type": "text", "text": f"paused {a['task_id']}"}]}
    if name == "resume_task": store.resume_task(a["task_id"]); return {"content": [{"type": "text", "text": f"resumed {a['task_id']}"}]}
    if name == "cancel_task":
        task = store.task(a["task_id"])
        key = (a["task_id"], task["worker_id"]) if task and task["worker_id"] else None
        run = ACTIVE_RUNS.pop(key, None) if key else None
        if run is not None:
            cancel_managed_worker(store, *key, run, a.get("reason", "supervisor cancellation"))
        else:
            store.cancel_task(a["task_id"], a.get("reason", "supervisor cancellation"))
        return {"content": [{"type": "text", "text": f"cancelled {a['task_id']}"}]}
    if name == "resolve_conflict":
        store.resolve_conflict(a["decision_id"], a["task_id"], a["decision"], a["reason"]); return {"content": [{"type": "text", "text": f"resolved {a['task_id']}"}]}
    if name == "inbox": return {"content": [{"type": "text", "text": json.dumps([dict(row) for row in store.inbox()])}]}
    raise ValueError(f"unknown tool: {name}")


def serve(state_path: str | Path) -> None:
    store = Store(state_path)
    try:
        for line in sys.stdin:
            if not line.strip(): continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as error:
                print(json.dumps({"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32700, "message": f"parse error: {error.msg}"}}), flush=True)
                continue
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
                print(json.dumps({"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                                  "error": {"code": -32600, "message": "invalid request"}}), flush=True)
                continue
            # JSON-RPC notifications, including MCP initialized, have no id
            # and must not receive a response.
            if "id" not in request:
                # JSON-RPC notifications never receive a response, including
                # custom no-id methods that the server may choose to ignore.
                try:
                    dispatch(store, request["method"], request.get("params", {}))
                except Exception:
                    pass
                continue
            response = {"jsonrpc": "2.0", "id": request.get("id")}
            try: response["result"] = dispatch(store, request["method"], request.get("params", {}))
            except Exception as error: response["error"] = {"code": -32000, "message": str(error)}
            print(json.dumps(response), flush=True)
    finally:
        # A client disconnect must not orphan provider processes owned by this
        # MCP server. Persist cancellation before closing the durable store.
        for key, run in list(ACTIVE_RUNS.items()):
            try:
                cancel_managed_worker(store, key[0], key[1], run, "MCP server shutdown")
            except Exception:
                run.cancel()
                run.wait()
            finally:
                ACTIVE_RUNS.pop(key, None)
        store.close()
