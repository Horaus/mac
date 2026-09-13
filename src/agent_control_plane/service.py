from __future__ import annotations

import subprocess
import uuid
import os
import signal
import json
import threading
import time
import fnmatch
import hashlib
import time
import re
import shutil
import tempfile
from pathlib import Path

from .providers import ManagedRun, ProviderAdapter, WorkerResult
from .profiles import ExecutionProfile, profile_from
from .result_contract import compact_result_summary

def _inject_context(prompt: str, context: str, profile: ExecutionProfile) -> str:
    if profile.context.allowed_paths:
        context += "\nALLOWED PATHS:\n" + "\n".join(profile.context.allowed_paths)
    if profile.context.max_input_tokens is not None:
        budget_chars = profile.context.max_input_tokens * 4
    combined = prompt + ("\n\n" + context if context else "")
    return combined[:profile.context.max_input_tokens * 4] if profile.context.max_input_tokens is not None else combined
from .store import Store, process_start_identity
from .authority import AuthorityPolicy, ContextPacket, RuntimeOperation, RuntimeReport, bound_output, classify_mutation, validate_runtime_operation, validate_runtime_mutation
from .provisioning import DependencyContract, verify_offline

HOST_INSTANCE_ID = f"host-{uuid.uuid4().hex}"

def _bounded_result(result: WorkerResult, profile: ExecutionProfile) -> WorkerResult:
    bounded = bound_output(result.output, profile.context.max_output_bytes, "provider.stdout")
    usage = dict(result.usage or {})
    usage.update({"output_omitted_bytes": bounded["omitted_bytes"], "output_provenance": bounded["provenance"]})
    usage["tool_output_bytes"] = max(int(usage.get("tool_output_bytes", 0)), len(result.output.encode("utf-8")))
    usage["soft_budget_exceeded"] = profile.context.max_input_tokens is not None and usage.get("input_tokens", 0) > profile.context.max_input_tokens
    return WorkerResult(result.exit_code, bounded["text"], result.session_id, usage, result.failure_class)

def _record_run_evidence(store: Store, run_id: str, result: WorkerResult, profile: ExecutionProfile,
                         cwd: Path, adapter: ProviderAdapter, prompt: str) -> None:
    """Compatibility shim: durable events are emitted by their owning paths."""
    return None

def _context_packet(store: Store, task_id: str, policy: AuthorityPolicy, supplied=None) -> ContextPacket:
    if isinstance(supplied, ContextPacket):
        return supplied
    task = store.task(task_id)
    goal_id = task["goal_id"] if task and task["goal_id"] else None
    goal = store.db.execute("SELECT id,title,status,worker_pack_id,inspection_mode FROM goals WHERE id=?", (goal_id,)).fetchone() if goal_id else None
    checkpoint = store.db.execute("SELECT id,sequence,state_json,next_action,recoverability FROM checkpoints WHERE goal_id=? ORDER BY sequence DESC LIMIT 1", (goal_id,)).fetchone() if goal_id else None
    memories = [dict(row) for row in store.db.execute("SELECT id,digest,version,source,status FROM memory_items WHERE namespace=? AND status IN ('CANDIDATE','WORKING','PROMOTED') AND tombstoned_at IS NULL ORDER BY created_at DESC LIMIT 20", (f"goal:{goal_id}",))] if goal_id else []
    current = {"task_id": task_id, "status": task["status"] if task else "unknown", "goal_id": goal_id,
               "goal": dict(goal) if goal else None, "checkpoint": dict(checkpoint) if checkpoint else None,
               "memory_refs": memories}
    return ContextPacket(1, task["title"] if task else task_id,
                         current,
                         authorizations=tuple(sorted(policy.capabilities)),
                         prohibited_actions=("accept_without_validation", "merge_worker_changes"),
                         resources=tuple(row["resource"] for row in store.db.execute("SELECT resource FROM task_resources WHERE task_id=?", (task_id,))),
                         acceptance_criteria=("worker result is reviewable",),
                         stop_conditions=("missing authority", "validation failure"),
                         response_schema={"status": "string", "evidence": "array"})

def _allowlist_manifest(cwd: Path, patterns: tuple[str, ...]) -> list[dict]:
    result = []
    for path in sorted(p for p in cwd.rglob("*") if p.is_file() and any(fnmatch.fnmatch(str(p.relative_to(cwd)), pattern) for pattern in patterns)):
        data = path.read_bytes()
        result.append({"path": str(path.relative_to(cwd)), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return result

def _restricted_context(cwd: Path, patterns: tuple[str, ...]) -> tuple[str, list[dict]]:
    """Build context solely from explicitly allowlisted files."""
    manifest = _allowlist_manifest(cwd, patterns)
    chunks = []
    for item in manifest:
        data = (cwd / item["path"]).read_text(errors="replace")
        chunks.append(f"FILE {item['path']}:\n{data}")
    return "\n\n".join(chunks), manifest

def _restricted_workspace(cwd: Path, manifest: list[dict]) -> Path:
    """Create a real read-only-input workspace containing only allowlisted files."""
    workspace = Path(tempfile.mkdtemp(prefix="mac-restricted-"))
    for item in manifest:
        source = cwd / item["path"]
        target = workspace / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return workspace

def _cleanup_restricted_workspace(workspace: Path | None) -> None:
    if workspace is not None:
        shutil.rmtree(workspace, ignore_errors=True)

def _dependency_preflight(store: Store, task, task_id: str, worker_id: str) -> None:
    execution = json.loads(task["execution_json"] or "{}")
    dependency = execution.get("dependency_contract")
    dependency_store = execution.get("dependency_store")
    if dependency and dependency_store:
        readiness = verify_offline(DependencyContract(**dependency), dependency_store)
        if not readiness["ready"]:
            store.set_task_status(task_id, "WAITING_DEPENDENCY")
            store.add_message("BLOCKER", readiness, task_id=task_id, worker_id=worker_id)
            raise RuntimeError(readiness["reason"])

def _budget_snapshot(profile: ExecutionProfile) -> dict:
    return {
        "prompt_context_bytes": None,
        "estimated_input_tokens": profile.context.max_input_tokens,
        "cumulative_input_tokens": profile.context.hard_input_tokens,
        "cached_input_tokens": None,
        "output_tokens": None,
        "tool_output_bytes": None,
        "files_read": None,
        "file_bytes": None,
        "commands": None,
        "model_turns": None,
        "wall_time_ms": None,
        "max_output_bytes": profile.context.max_output_bytes,
        "enforcement": "hard-stream-and-soft-post-run" if profile.context.hard_input_tokens is not None else "soft-post-run",
    }

def authorize_runtime_action(store: Store, policy: AuthorityPolicy, capability: str,
                             report: RuntimeReport | None, resource: str | None = None,
                             task_id: str | None = None, worker_id: str | None = None,
                             active_job: bool = False, target_id: str | None = None,
                             project_id: str | None = None, operation: RuntimeOperation | str | None = None) -> None:
    try:
        validate_runtime_operation(capability, operation)
        validate_runtime_mutation(policy, capability, report, active_job, target_id, project_id)
        if capability != "browser.observe" and not all((resource, task_id, worker_id)):
            raise PermissionError("runtime mutation requires an exclusive lease scope")
        if resource and task_id and worker_id and capability != "browser.observe":
            if not store.acquire(resource, task_id, worker_id, "WRITE"):
                raise PermissionError("exclusive runtime mutation lease unavailable")
    except Exception as error:
        if task_id:
            latest = store.latest_run(task_id)
            if latest:
                store._emit_runtime_denied(latest["id"], {"capability": capability, "reason": str(error), "target_id": target_id, "resource": resource})
        raise
    if task_id:
        latest = store.latest_run(task_id)
        if latest:
            store._emit_runtime_granted(latest["id"], {"capability": capability, "operation": getattr(operation, "value", operation), "target_id": target_id, "project_id": project_id, "resource": resource})

def authorize_action(store: Store, policy: AuthorityPolicy, capability: str, payload,
                     token_id: str | None = None, destructive: bool = False, external: bool = False,
                     project_id: str | None = None, provider_id: str | None = None,
                     job_id: str | None = None, idempotency_key: str | None = None,
                     run_id: str | None = None, task_id: str | None = None) -> None:
    """Supervisor gate used by costly mutation surfaces before side effects."""
    policy.require(capability)
    if classify_mutation(capability, destructive, external).value == "costly_destructive_external":
        if not token_id:
            raise PermissionError("scoped approval token required")
        if not all((project_id, provider_id, job_id, idempotency_key, run_id, task_id)):
            raise PermissionError("approval scope is mandatory")
        store.consume_approval(token_id, policy, payload, project_id, provider_id, job_id, idempotency_key, run_id, task_id)

def submit_provider(store: Store, policy: AuthorityPolicy, provider_id: str, job_id: str,
                    payload: dict, submit, *, paid: bool = False, approval_scope: dict | None = None):
    capability = "provider.paid_submit" if paid else "provider.free_submit"
    scope = approval_scope or {}
    authorize_action(store, policy, capability, payload, destructive=paid, external=paid,
                     token_id=scope.get("token_id"),
                     project_id=scope.get("project_id"), provider_id=provider_id, job_id=job_id,
                     idempotency_key=scope.get("idempotency_key"), run_id=scope.get("run_id"), task_id=scope.get("task_id"))
    return submit(payload)

def publish_external(store: Store, policy: AuthorityPolicy, payload: dict, publish, **scope):
    authorize_action(store, policy, "external.publish", payload, external=True, **scope)
    return publish(payload)

def delete_destructive(store: Store, policy: AuthorityPolicy, target: str, delete, **scope):
    authorize_action(store, policy, "destructive.delete", {"target": target}, destructive=True, **scope)
    return delete(target)

def report_blocker(store: Store, task_id: str, blocker: str, external_state_version: str,
                   threshold: int = 3) -> bool:
    blocked = store.record_blocker(task_id, blocker, external_state_version, threshold)
    if blocked and store.task(task_id)["status"] in {"READY", "RUNNING", "PAUSED"}:
        store.set_task_status(task_id, "WAITING_DECISION")
        store.add_message("BLOCKER", {"blocker": blocker, "external_state_version": external_state_version,
                                      "action": "automatic continuation stopped"}, task_id=task_id)
    return blocked


def run_worker(store: Store, task_id: str, worker_id: str, adapter: ProviderAdapter,
               prompt: str, cwd: str | Path, base_commit: str | None = None,
               profile: ExecutionProfile | None = None, authority=None, context=None,
               conversation_id: str | None = None, resume_session_id: str | None = None,
               context_checkpoint: dict | None = None, resume_from_task_id: str | None = None,
               preserve_resume_snapshot: bool = False) -> WorkerResult:
    """Run one provider worker and record a reviewable result; never integrate it."""
    task = store.task(task_id)
    if task is None:
        raise ValueError(f"unknown task: {task_id}")
    store.add_worker(worker_id, adapter.name, worktree=str(cwd))
    if resume_session_id and preserve_resume_snapshot:
        source = store.latest_run(resume_from_task_id or task_id, None if resume_from_task_id else worker_id)
        profile = profile_from(json.loads(source["profile_json"])) if source and source["profile_json"] else (profile or ExecutionProfile())
    else:
        profile = profile or ExecutionProfile()
    adapter.validate_profile(profile)
    resolved_authority = authority or AuthorityPolicy()
    resolved_authority.require("provider.preflight")
    packet = _context_packet(store, task_id, resolved_authority, context)
    if context_checkpoint:
        packet = ContextPacket(packet.version, packet.objective, {**packet.current_state, "checkpoint": context_checkpoint},
                               packet.authorizations, packet.prohibited_actions, packet.resources, packet.leases,
                               packet.relevant_files, packet.runtime, packet.acceptance_criteria, packet.stop_conditions, packet.response_schema)
    restricted_manifest = None
    restricted_workspace = None
    if profile.context.allowed_paths:
        raw_context, restricted_manifest = _restricted_context(Path(cwd), profile.context.allowed_paths)
        restricted_workspace = _restricted_workspace(Path(cwd), restricted_manifest)
    else:
        raw_context = store.knowledge_context(profile.context.knowledge_mode)
    # `max_input_tokens` remains the prompt/context estimate; the serialized
    # authority packet is persisted and sent separately, and provider usage is
    # what determines observed/hard enforcement.  Do not turn a soft budget
    # into a claim-blocking limit merely because the packet has fixed metadata.
    if profile.context.max_input_tokens is not None and len((prompt + ("\n\n" + raw_context if raw_context else "")).encode("utf-8")) > profile.context.max_input_tokens * 4:
        raise ValueError("prompt/context packet exceeds preflight budget")
    prompt = _inject_context(prompt, raw_context, profile)
    _dependency_preflight(store, task, task_id, worker_id)
    if resume_session_id:
        # This is the synchronous compatibility facade, but it must use the
        # provider-native resume boundary rather than merely persisting a
        # session id and calling adapter.run().
        if context_checkpoint:
            prompt += "\n\nSUPERVISOR CONTEXT CHECKPOINT:\n" + json.dumps(context_checkpoint, sort_keys=True)
        resumed = resume_managed_worker(store, task_id, worker_id, adapter, resume_session_id, prompt, cwd,
                                         profile, resume_from_task_id=resume_from_task_id,
                                         authority=resolved_authority, context=context,
                                         context_checkpoint=context_checkpoint)
        store.record_run_authority(resumed.run_id, resolved_authority, packet)
        started_at = time.monotonic()
        try:
            result = resumed.wait()
        except Exception:
            store.set_run_phase(resumed.run_id, "FAILED")
            store.set_task_status(task_id, "FAILED")
            raise
        result = _bounded_result(result, profile)
        prompt_bytes = len(prompt.encode("utf-8"))
        result.usage.update({"prompt_bytes": prompt_bytes, "prompt_context_bytes": prompt_bytes, "wall_time_ms": int((time.monotonic() - started_at) * 1000)})
        status = "REVIEW" if result.exit_code == 0 else "FAILED"
        store.set_worker_status(worker_id, "COMPLETED" if result.exit_code == 0 else "FAILED", result.session_id)
        store.finish_run(resumed.run_id, "COMPLETED" if result.exit_code == 0 else "FAILED", result.exit_code, result.output, result.session_id, result.failure_class, result.usage)
        store.set_task_status(task_id, status); store.release_task_leases(task_id)
        store.add_message("TASK_COMPLETE" if result.exit_code == 0 else "BLOCKER", {"exit_code": result.exit_code, "output": result.output}, task_id, worker_id)
        return result
    # All deterministic preflight work happens before claiming the task.
    store.claim_task(task_id, worker_id, base_commit)
    store.capture_declared_resources(task_id)
    run_id = f"run-{uuid.uuid4().hex}"
    budget = _budget_snapshot(profile)
    store.start_run(run_id, task_id, worker_id, adapter.name, profile=profile, conversation_id=conversation_id,
                    provider_session_id=resume_session_id, resume_session_id=resume_session_id,
                    context_checkpoint=context_checkpoint, requested_budget=budget, effective_budget=budget,
                    enforcement_source="mac-preflight", budget_mode="hard" if profile.context.hard_input_tokens is not None else "soft")
    store.record_run_archetype(run_id)
    if restricted_manifest is not None:
        store.record_context_manifest(run_id, restricted_manifest)
    store.record_run_authority(run_id, resolved_authority, packet)
    try:
        started_at = time.monotonic()
        try:
            try:
                result = adapter.run(prompt, restricted_workspace or Path(cwd), profile, context_packet=packet)
            except TypeError as context_error:
                if "context_packet" not in str(context_error): raise
                result = adapter.run(prompt, restricted_workspace or Path(cwd), profile)
        except TypeError as error:
            if "positional argument" not in str(error): raise
            result = adapter.run(prompt, restricted_workspace or Path(cwd))
    except Exception:
        store.finish_run(run_id, "FAILED", 1, "provider raised an exception")
        store.set_worker_status(worker_id, "FAILED")
        store.set_task_status(task_id, "FAILED")
        store.release_task_leases(task_id)
        raise
    result = _bounded_result(result, profile)
    prompt_bytes = len(prompt.encode("utf-8"))
    result.usage.update({"prompt_bytes": prompt_bytes, "prompt_context_bytes": prompt_bytes, "wall_time_ms": int((time.monotonic() - started_at) * 1000)})
    store.set_worker_status(worker_id, "COMPLETED" if result.exit_code == 0 else "FAILED", result.session_id)
    store.finish_run(run_id, "COMPLETED" if result.exit_code == 0 else "FAILED", result.exit_code, result.output, result.session_id, result.failure_class, result.usage)
    if result.usage.get("soft_budget_exceeded"):
        store.add_message("BLOCKER", {"reason": "soft context budget exceeded", "input_tokens": result.usage.get("input_tokens")}, task_id=task_id, worker_id=worker_id)
    _record_run_evidence(store, run_id, result, profile, Path(cwd), adapter, prompt)
    status = store.settle_task_result(task_id, result.exit_code == 0)
    store.release_task_leases(task_id)
    event_type = "QUESTION" if status == "WAITING_DECISION" else ("TASK_COMPLETE" if result.exit_code == 0 else "BLOCKER")
    store.add_message(event_type,
                      {"exit_code": result.exit_code, "summary": compact_result_summary(result.output), "evidence_id": f"run:{run_id}"}, task_id, worker_id)
    _cleanup_restricted_workspace(restricted_workspace)
    return result


def validate(store: Store, task_id: str, command: str, cwd: str | Path, timeout: float = 300, authority=None) -> int:
    if timeout <= 0:
        raise ValueError("validation timeout must be positive")
    if re.search(r"(?:pnpm|npm|yarn|pip|uv)\s+(?:install|add|fetch|update|sync|download)\b", command.lower()):
        (authority or AuthorityPolicy()).require("dependency.install")
    process = subprocess.Popen(command, cwd=cwd, shell=True, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        exit_code = process.returncode
        output = stdout + stderr
    except subprocess.TimeoutExpired as error:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        chunks = [error.stdout, error.stderr, stdout, stderr]
        output = "".join(chunk.decode(errors="replace") if isinstance(chunk, bytes) else (chunk or "") for chunk in chunks)
        exit_code = 124
        output += "\nvalidation timeout\n"
    store.add_validation(task_id, command, exit_code, output)
    return exit_code


def start_managed_worker(store: Store, task_id: str, worker_id: str, adapter: ProviderAdapter,
                         prompt: str, cwd: str | Path, profile: ExecutionProfile | None = None,
                         authority=None, context=None) -> ManagedRun:
    task = store.task(task_id)
    if task is None: raise ValueError(f"unknown task: {task_id}")
    profile = profile or ExecutionProfile()
    adapter.validate_profile(profile)
    resolved_authority = authority or AuthorityPolicy()
    resolved_authority.require("provider.preflight")
    packet = _context_packet(store, task_id, resolved_authority, context)
    restricted_manifest = None
    restricted_workspace = None
    if profile.context.allowed_paths:
        raw_context, restricted_manifest = _restricted_context(Path(cwd), profile.context.allowed_paths)
        restricted_workspace = _restricted_workspace(Path(cwd), restricted_manifest)
    else:
        raw_context = store.knowledge_context(profile.context.knowledge_mode)
    if profile.context.max_input_tokens is not None and len((prompt + ("\n\n" + raw_context if raw_context else "")).encode("utf-8")) > profile.context.max_input_tokens * 4:
        raise ValueError("prompt/context packet exceeds preflight budget")
    prompt = _inject_context(prompt, raw_context, profile)
    _dependency_preflight(store, task, task_id, worker_id)
    store.claim_task(task_id, worker_id)
    try:
        try:
            run = adapter.start(prompt, restricted_workspace or Path(cwd), profile, context_packet=packet)
        except TypeError as context_error:
            if "context_packet" not in str(context_error): raise
            run = adapter.start(prompt, restricted_workspace or Path(cwd), profile)
    except Exception:
        store.set_task_status(task_id, "FAILED")
        raise
    store.add_worker(worker_id, adapter.name, run.session_id, str(cwd))
    run.run_id = f"run-{uuid.uuid4().hex}"
    run.hard_input_tokens = getattr(profile.context, "hard_input_tokens", None)
    budget = _budget_snapshot(profile)
    store.start_run(run.run_id, task_id, worker_id, adapter.name, run.session_id, profile,
                    provider_session_id=run.session_id,
                    requested_budget=budget, effective_budget=budget, enforcement_source="mac-preflight",
                    budget_mode="hard" if profile.context.hard_input_tokens is not None else "soft")
    store.record_run_archetype(run.run_id)
    if restricted_manifest is not None:
        store.record_context_manifest(run.run_id, restricted_manifest)
    store.bind_process_identity(run.run_id, HOST_INSTANCE_ID, run.process.pid, process_start_identity(run.process.pid), time.time() + 30)
    store.set_run_phase(run.run_id, "PROVIDER_RUNNING")
    store.record_run_authority(run.run_id, resolved_authority, packet)
    run.restricted_workspace = restricted_workspace
    def heartbeat_loop():
        heartbeat_store = Store(store.path)
        while run.process.poll() is None:
            try:
                heartbeat_store.heartbeat(run.run_id, "provider")
            except Exception:
                heartbeat_store.close()
                return
            time.sleep(0.25)
        heartbeat_store.close()
    threading.Thread(target=heartbeat_loop, name=f"mac-heartbeat-{run.run_id}", daemon=True).start()
    return run


def finish_managed_worker(store: Store, task_id: str, worker_id: str, run: ManagedRun) -> WorkerResult:
    started_at = time.monotonic()
    result = run.wait()
    durable_run = store.db.execute("SELECT status FROM runs WHERE id=?", (run.run_id,)).fetchone()
    durable_task = store.task(task_id)
    # A waiter can race with supervisor cancellation/terminal reconciliation.
    # Once durable state is terminal, late provider output is never allowed to
    # reopen the task or emit TASK_COMPLETE.
    if durable_run and durable_run["status"] in {"CANCELLED", "FAILED", "ORPHANED"} or durable_task and durable_task["status"] in {"FAILED", "DONE", "ACCEPTED"}:
        if durable_run and durable_run["status"] == "RUNNING":
            store.finish_run(run.run_id, "CANCELLED", 130, result.output, result.session_id, "LATE_TERMINAL_RESULT")
        store.release_task_leases(task_id)
        store.add_message("LATE_RESULT_IGNORED", {"run_id": run.run_id, "exit_code": result.exit_code, "reason": "durable terminal state"}, task_id, worker_id)
        _cleanup_restricted_workspace(getattr(run, "restricted_workspace", None))
        return WorkerResult(130, result.output, result.session_id, result.usage or {}, "LATE_TERMINAL_RESULT")
    store.heartbeat(run.run_id, "finish")
    profile = profile_from(json.loads(store.db.execute("SELECT profile_json FROM runs WHERE id=?", (run.run_id,)).fetchone()[0]))
    result = _bounded_result(result, profile)
    prompt_bytes = len("managed run".encode())
    result.usage.update({"prompt_bytes": prompt_bytes, "prompt_context_bytes": prompt_bytes, "wall_time_ms": int((time.monotonic() - started_at) * 1000)})
    store.set_worker_status(worker_id, "COMPLETED" if result.exit_code == 0 else "FAILED", result.session_id)
    store.finish_run(run.run_id, "COMPLETED" if result.exit_code == 0 else "FAILED", result.exit_code, result.output, result.session_id, result.failure_class, result.usage)
    if result.usage.get("soft_budget_exceeded"):
        store.add_message("BLOCKER", {"reason": "soft context budget exceeded", "input_tokens": result.usage.get("input_tokens")}, task_id=task_id, worker_id=worker_id)
    _record_run_evidence(store, run.run_id, result, profile, run.cwd, run.adapter, "managed run")
    status = store.settle_task_result(task_id, result.exit_code == 0)
    store.release_task_leases(task_id)
    # A worker-authored decision request is already the authoritative outcome.
    # Do not overwrite its exact type with a synthetic QUESTION when the
    # provider process subsequently exits.
    pending_decision = store.db.execute(
        "SELECT type FROM messages WHERE task_id=? AND type IN ('QUESTION','DECISION_REQUIRED') ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if status == "WAITING_DECISION" and pending_decision is None:
        store.add_message("QUESTION",
                          {"exit_code": result.exit_code, "summary": compact_result_summary(result.output), "evidence_id": f"run:{run.run_id}"}, task_id, worker_id)
    elif status != "WAITING_DECISION":
        event_type = "TASK_COMPLETE" if result.exit_code == 0 else "BLOCKER"
        store.add_message(event_type,
                          {"exit_code": result.exit_code, "summary": compact_result_summary(result.output), "evidence_id": f"run:{run.run_id}"}, task_id, worker_id)
    _cleanup_restricted_workspace(getattr(run, "restricted_workspace", None))
    return result


def pause_managed_worker(store: Store, task_id: str, worker_id: str, run: ManagedRun,
                         reason: str = "supervisor pause") -> None:
    """Pause the live process and make the durable task state observable."""
    store.heartbeat(run.run_id, "pause")
    run.pause()
    store.set_worker_status(worker_id, "PAUSED", run.session_id)
    store.pause_task(task_id, reason)


def resume_live_worker(store: Store, task_id: str, worker_id: str, run: ManagedRun) -> None:
    """Continue a paused live process without creating a new provider session."""
    run.resume()
    store.set_worker_status(worker_id, "RUNNING", run.session_id)
    store.set_task_status(task_id, "RUNNING")


def cancel_managed_worker(store: Store, task_id: str, worker_id: str, run: ManagedRun,
                          reason: str = "supervisor cancellation") -> None:
    """Stop a live provider process and persist cancellation as recoverable failure."""
    run.cancel()
    result = run.wait(timeout=5)
    store.set_worker_status(worker_id, "FAILED")
    store.finish_run(run.run_id, "CANCELLED", 130, result.output or reason, run.session_id)
    store.cancel_task(task_id, reason)
    _cleanup_restricted_workspace(getattr(run, "restricted_workspace", None))


def resume_managed_worker(store: Store, task_id: str, worker_id: str, adapter: ProviderAdapter,
                          session_id: str, prompt: str, cwd: str | Path, profile: ExecutionProfile | None = None,
                          resume_from_task_id: str | None = None, authority=None, context=None,
                          context_checkpoint: dict | None = None) -> ManagedRun:
    task = store.task(task_id)
    if task is None: raise ValueError(f"unknown task: {task_id}")
    # A provider session may be handed to a different worker; source-task
    # history, not the destination worker identity, owns the snapshot.
    previous = store.latest_run(resume_from_task_id or task_id, None if resume_from_task_id else worker_id)
    snapshot = profile_from(json.loads(previous["profile_json"])) if previous and previous["profile_json"] else ExecutionProfile()
    from_snapshot = profile is None
    if profile is None:
        profile = snapshot
    elif profile.snapshot() != snapshot.snapshot():
        # Unsupported resume fields are valid historical facts, but not valid
        # reconfiguration requests. A supervisor must explicitly request a
        # supported, changed profile instead of silently changing a session.
        if profile.profile != snapshot.profile or profile.sandbox != snapshot.sandbox:
            raise ValueError("resume override changes unsupported Codex session settings")
    adapter.validate_profile(profile)
    resolved_authority = authority or AuthorityPolicy()
    resolved_authority.require("provider.preflight")
    packet = _context_packet(store, task_id, resolved_authority, context)
    if context_checkpoint:
        packet = ContextPacket(packet.version, packet.objective, {**packet.current_state, "checkpoint": context_checkpoint},
                               packet.authorizations, packet.prohibited_actions, packet.resources, packet.leases,
                               packet.relevant_files, packet.runtime, packet.acceptance_criteria, packet.stop_conditions, packet.response_schema)
    resume_snapshot = from_snapshot or profile.snapshot() == snapshot.snapshot()
    checkpoint_fallback = False
    if adapter.__class__.resume is ProviderAdapter.resume:
        if context_checkpoint is None:
            if previous: store.set_run_phase(previous["id"], "ORPHANED")
            if task["status"] == "READY": store.set_task_status(task_id, "WAITING_DECISION")
            store.add_message("BLOCKER", {"reason": "provider session cannot resume", "session_id": session_id, "recovery": "start a new session with context_checkpoint"}, task_id=task_id, worker_id=worker_id)
            raise RuntimeError("provider cannot resume session; recovery requires a new context checkpoint")
        # Native continuity is unavailable, so a supervisor-supplied,
        # durable checkpoint permits an explicit new-session recovery.  This
        # is deliberately not inferred from the old prompt or session id.
        required_checkpoint = ("worker_pack", "working_memory", "goal_restatement", "prohibited_actions")
        missing_checkpoint = [key for key in required_checkpoint if not context_checkpoint.get(key)]
        if missing_checkpoint:
            if previous:
                store.set_run_phase(previous["id"], "ORPHANED")
            if task["status"] == "READY":
                store.set_task_status(task_id, "WAITING_DECISION")
            store.add_message("BLOCKER", {"reason": "checkpoint reconciliation failed",
                                           "missing": missing_checkpoint,
                                           "recovery": "supply complete worker pack, working memory, goal restatement, and prohibited actions"},
                              task_id=task_id, worker_id=worker_id)
            raise RuntimeError("checkpoint fallback requires complete reconciliation fields")
        store.add_message("SESSION_FALLBACK", {
            "old_session_id": session_id,
            "recovery": "new provider session",
            "checkpoint_supplied": True,
            "worker_pack_injected": bool(context_checkpoint.get("worker_pack") or context_checkpoint.get("worker_pack_id")),
            "working_memory_injected": bool(context_checkpoint.get("working_memory")),
            "goal_restatement_required": True,
            "goal_restatement_present": True,
            "prohibited_actions_injected": True,
            "disclosure": "conversation memory restored from supervisor checkpoint; provider session was not resumed",
        }, task_id=task_id, worker_id=worker_id)
        checkpoint_fallback = True
    # Resume is still a new managed execution for the destination task. Claim
    # it and capture declared resources before invoking the provider boundary.
    store.claim_task(task_id, worker_id)
    store.capture_declared_resources(task_id)
    try:
        if checkpoint_fallback:
            try:
                run = adapter.start(prompt, Path(cwd), profile, context_packet=packet)
            except TypeError as error:
                if "context_packet" not in str(error):
                    raise
                run = adapter.start(prompt, Path(cwd), profile)
        else:
            run = adapter.resume(session_id, prompt, Path(cwd), profile, resume_snapshot=resume_snapshot)
    except NotImplementedError as error:
        if previous:
            store.set_run_phase(previous["id"], "ORPHANED")
        if task["status"] == "READY":
            store.set_task_status(task_id, "WAITING_DECISION")
        store.add_message("BLOCKER", {"reason": "provider session cannot resume", "session_id": session_id, "recovery": "start a new session with context_checkpoint"}, task_id=task_id, worker_id=worker_id)
        raise RuntimeError("provider cannot resume session; recovery requires a new context checkpoint") from error
    except TypeError as error:
        if "positional argument" not in str(error) and "unexpected keyword" not in str(error): raise
        try:
            run = adapter.resume(session_id, prompt, Path(cwd), profile)
        except TypeError as legacy_error:
            if "positional argument" not in str(legacy_error): raise
            run = adapter.resume(session_id, prompt, Path(cwd))
    run.run_id = f"run-{uuid.uuid4().hex}"
    run.hard_input_tokens = getattr(profile.context, "hard_input_tokens", None)
    budget = _budget_snapshot(profile)
    store.start_run(run.run_id, task_id, worker_id, adapter.name, run.session_id, profile,
                    provider_session_id=session_id, resume_session_id=session_id,
                    context_checkpoint=context_checkpoint,
                    requested_budget=budget, effective_budget=budget, enforcement_source="mac-preflight",
                    budget_mode="hard" if profile.context.hard_input_tokens is not None else "soft")
    store.record_run_archetype(run.run_id)
    if profile.context.allowed_paths:
        store.record_context_manifest(run.run_id, _allowlist_manifest(Path(cwd), profile.context.allowed_paths))
    store.record_run_authority(run.run_id, resolved_authority, packet)
    store.set_worker_status(worker_id, "RUNNING", run.session_id)
    store.set_task_status(task_id, "RUNNING")
    return run


def arbitrate_conflict(store: Store, task_id: str, decision_id: str, reviewer: ProviderAdapter,
                       evidence: str, cwd: str | Path) -> WorkerResult:
    """Ask a reviewer provider for a decision, then persist it through supervisor APIs."""
    prompt = ("You are the supervisor's conflict reviewer. Return a concise decision and reason.\n"
              f"Task: {task_id}\nEvidence:\n{evidence}")
    result = reviewer.run(prompt, Path(cwd))
    if result.exit_code != 0:
        store.add_message("BLOCKER", {"reviewer_output": result.output}, task_id=task_id)
        raise RuntimeError("conflict reviewer failed")
    store.resolve_conflict(decision_id, task_id, result.output.strip(), "reviewer provider decision")
    return result
