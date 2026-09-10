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


TOOL_DEFS = {
    "status": {}, "inbox": {}, "reconcile": {}, "managed_queue_status": {},
    "control_status": {},
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
    "create_task": {"id": {"type": "string"}, "title": {"type": "string"}, "provider": {"type": "string"}, "depends_on": {"type": "array", "items": {"type": "string"}}, "execution": {"type": "object"}},
    "create_resource": {"name": {"type": "string"}, "kind": {"type": "string"}, "paths": {"type": "array", "items": {"type": "string"}}},
    "send_message": {"type": {"type": "string"}, "payload": {"type": "object"}, "task_id": {"type": "string"}, "worker_id": {"type": "string"}},
    "accept_task": {"task_id": {"type": "string"}}, "pause_task": {"task_id": {"type": "string"}, "reason": {"type": "string"}},
    "resume_task": {"task_id": {"type": "string"}}, "cancel_task": {"task_id": {"type": "string"}, "reason": {"type": "string"}},
    "resolve_conflict": {"decision_id": {"type": "string"}, "task_id": {"type": "string"}, "decision": {"type": "string"}, "reason": {"type": "string"}},
    "run_worker": {"task_id": {"type": "string"}, "worker_id": {"type": "string"}, "provider": {"type": "string"}, "prompt": {"type": "string"}, "cwd": {"type": "string"}, "conversation_id": {"type": "string"}, "resume_session_id": {"type": "string"}, "resume_from_task_id": {"type": "string"}, "context_checkpoint": {"type": "object"}, "execution": {"type": "object"}, "wait": {"type": "boolean"}},
    "start_worker": {"task_id": {"type": "string"}, "worker_id": {"type": "string"}, "provider": {"type": "string"}, "prompt": {"type": "string"}, "cwd": {"type": "string"}, "conversation_id": {"type": "string"}, "resume_session_id": {"type": "string"}, "context_checkpoint": {"type": "object"}, "execution": {"type": "object"}, "resources": {"type": "array"}},
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
}

# Managed runs are deliberately process-local: the MCP server owns the
# subprocess handle, while durable task/run state remains in SQLite.
ACTIVE_RUNS = {}
ACTIVE_QUEUE_IDS = {}
MANAGED_SCHEDULERS = {}
MANAGED_FUTURES = {}
MCP_RECOVERY_LOCK = __import__("threading").RLock()

def _managed_scheduler(store: Store, capacity: int) -> ManagedScheduler:
    """Return the process-wide scheduler for a durable state database."""
    key = str(store.path.resolve())
    scheduler = MANAGED_SCHEDULERS.get(key)
    if scheduler is None or scheduler.max_workers != capacity:
        if scheduler is not None:
            scheduler.close()
        scheduler = ManagedScheduler(store, capacity)
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


def dispatch(store: Store, method: str, args: dict) -> object:
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
                "serverInfo": {"name": "agent-control-plane", "version": "0.4.0"},
                "instructions": f"Startup reconciliation recovered {recovered_count} run(s). You are the Master supervisor. Use MAC Control to register your master_id and request a complete worker group before dispatch. In lock mode, worker capacity is fixed and shortages return PENDING/rejected; flexible mode permits temporary worker counts but does not persist chat history. In lock mode preserve master_id and conversation_id for history; do not silently continue a changed conversation. Use isolated worktrees, never merge worker changes, validate before acceptance, and report status, validation, commit, blockers, and conflicts."}
    if method == "tools/list":
        return {"tools": [{"name": name, "description": f"control-plane {name}",
                            "inputSchema": {"type": "object", "properties": props}}
                         for name, props in TOOL_DEFS.items()]}
    if method != "tools/call":
        raise ValueError(f"unsupported method: {method}")
    name, a = args.get("name"), args.get("arguments", {})
    if name == "status": return {"content": [{"type": "text", "text": json.dumps(store.snapshot())}]}
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
    if name == "control_status": return {"content": [{"type": "text", "text": json.dumps(store.control_snapshot(), ensure_ascii=False)}]}
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
            return {"content": [{"type": "text", "text": json.dumps({"exit_code": result.exit_code, "session_id": result.session_id, "status": store.task(a["task_id"])["status"]})}]}
        run = ACTIVE_RUNS.pop(key, None)
        if run is None: raise ValueError("managed worker is not active")
        result = finish_managed_worker(store, *key, run)
        if key in ACTIVE_QUEUE_IDS: store.mark_managed_finished(ACTIVE_QUEUE_IDS.pop(key))
        config_path = store.path.parent / "config.json"
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
        with MCP_RECOVERY_LOCK:
            _managed_scheduler(store, int(config.get("execution", {}).get("max_concurrent_workers", 2))).recover_queued(
                provider, profile_from,
                lambda value: AuthorityPolicy(ExecutionMode(value.get("mode", "isolated_sandbox")), frozenset(value.get("capabilities", []))),
                _context_from_snapshot, lambda queue_id, job: _recover_live_job(store, queue_id, job))
        return {"content": [{"type": "text", "text": json.dumps({"exit_code": result.exit_code, "session_id": result.session_id, "status": store.task(a["task_id"])["status"]})}]}
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
        store.add_task(a["id"], a["title"], a.get("provider"), a.get("depends_on", []), a.get("execution", {})); return {"content": [{"type": "text", "text": f"created {a['id']}"}]}
    if name == "create_resource":
        store.add_resource(a["name"], a["kind"], a.get("paths", [])); return {"content": [{"type": "text", "text": f"created {a['name']}"}]}
    if name == "send_message":
        store.add_message(a["type"], a.get("payload", {}), a.get("task_id"), a.get("worker_id")); return {"content": [{"type": "text", "text": "queued"}]}
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
