from __future__ import annotations

import subprocess
import uuid
import os
import signal
import json
from pathlib import Path

from .providers import ManagedRun, ProviderAdapter, WorkerResult
from .profiles import ExecutionProfile, profile_from

def _inject_context(prompt: str, context: str, profile: ExecutionProfile) -> str:
    if profile.context.allowed_paths:
        context += "\nALLOWED PATHS:\n" + "\n".join(profile.context.allowed_paths)
    if profile.context.max_input_tokens is not None:
        budget_chars = profile.context.max_input_tokens * 4
        context = context[:budget_chars]
    return prompt + ("\n\n" + context if context else "")
from .store import Store
from .authority import AuthorityPolicy, ContextPacket, RuntimeOperation, RuntimeReport, bound_output, classify_mutation, validate_runtime_operation, validate_runtime_mutation

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
    return ContextPacket(1, task["title"] if task else task_id,
                         {"task_id": task_id, "status": task["status"] if task else "unknown"},
                         authorizations=tuple(sorted(policy.capabilities)),
                         prohibited_actions=("accept_without_validation", "merge_worker_changes"),
                         resources=tuple(row["resource"] for row in store.db.execute("SELECT resource FROM task_resources WHERE task_id=?", (task_id,))),
                         acceptance_criteria=("worker result is reviewable",),
                         stop_conditions=("missing authority", "validation failure"),
                         response_schema={"status": "string", "evidence": "array"})

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
               profile: ExecutionProfile | None = None, authority=None, context=None) -> WorkerResult:
    """Run one provider worker and record a reviewable result; never integrate it."""
    task = store.task(task_id)
    if task is None:
        raise ValueError(f"unknown task: {task_id}")
    store.add_worker(worker_id, adapter.name, worktree=str(cwd))
    profile = profile or ExecutionProfile()
    adapter.validate_profile(profile)
    resolved_authority = authority or AuthorityPolicy()
    resolved_authority.require("provider.preflight")
    store.claim_task(task_id, worker_id, base_commit)
    store.capture_declared_resources(task_id)
    packet = _context_packet(store, task_id, resolved_authority, context)
    prompt = _inject_context(prompt, store.knowledge_context(profile.context.knowledge_mode), profile)
    run_id = f"run-{uuid.uuid4().hex}"
    store.start_run(run_id, task_id, worker_id, adapter.name, profile=profile)
    store.record_run_authority(run_id, resolved_authority, packet)
    try:
        try:
            try:
                result = adapter.run(prompt, Path(cwd), profile, context_packet=packet)
            except TypeError as context_error:
                if "context_packet" not in str(context_error): raise
                result = adapter.run(prompt, Path(cwd), profile)
        except TypeError as error:
            if "positional argument" not in str(error): raise
            result = adapter.run(prompt, Path(cwd))
    except Exception:
        store.finish_run(run_id, "FAILED", 1, "provider raised an exception")
        store.set_worker_status(worker_id, "FAILED")
        store.set_task_status(task_id, "FAILED")
        store.release_task_leases(task_id)
        raise
    result = _bounded_result(result, profile)
    status = "REVIEW" if result.exit_code == 0 else "FAILED"
    store.set_worker_status(worker_id, "COMPLETED" if result.exit_code == 0 else "FAILED", result.session_id)
    store.finish_run(run_id, "COMPLETED" if result.exit_code == 0 else "FAILED", result.exit_code, result.output, result.session_id, result.failure_class, result.usage)
    if result.usage.get("soft_budget_exceeded"):
        store.add_message("BLOCKER", {"reason": "soft context budget exceeded", "input_tokens": result.usage.get("input_tokens")}, task_id=task_id, worker_id=worker_id)
    _record_run_evidence(store, run_id, result, profile, Path(cwd), adapter, prompt)
    store.set_task_status(task_id, status)
    store.release_task_leases(task_id)
    store.add_message("TASK_COMPLETE" if result.exit_code == 0 else "BLOCKER",
                      {"exit_code": result.exit_code, "output": result.output}, task_id, worker_id)
    return result


def validate(store: Store, task_id: str, command: str, cwd: str | Path, timeout: float = 300) -> int:
    if timeout <= 0:
        raise ValueError("validation timeout must be positive")
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
    store.claim_task(task_id, worker_id)
    packet = _context_packet(store, task_id, resolved_authority, context)
    prompt = _inject_context(prompt, store.knowledge_context(profile.context.knowledge_mode), profile)
    try:
        try:
            run = adapter.start(prompt, Path(cwd), profile, context_packet=packet)
        except TypeError as context_error:
            if "context_packet" not in str(context_error): raise
            run = adapter.start(prompt, Path(cwd), profile)
    except Exception:
        store.set_task_status(task_id, "FAILED")
        raise
    store.add_worker(worker_id, adapter.name, run.session_id, str(cwd))
    run.run_id = f"run-{uuid.uuid4().hex}"
    store.start_run(run.run_id, task_id, worker_id, adapter.name, run.session_id, profile)
    store.record_run_authority(run.run_id, resolved_authority, packet)
    return run


def finish_managed_worker(store: Store, task_id: str, worker_id: str, run: ManagedRun) -> WorkerResult:
    result = run.wait()
    store.heartbeat(run.run_id, "finish")
    profile = profile_from(json.loads(store.db.execute("SELECT profile_json FROM runs WHERE id=?", (run.run_id,)).fetchone()[0]))
    result = _bounded_result(result, profile)
    status = "REVIEW" if result.exit_code == 0 else "FAILED"
    store.set_worker_status(worker_id, "COMPLETED" if result.exit_code == 0 else "FAILED", result.session_id)
    store.finish_run(run.run_id, "COMPLETED" if result.exit_code == 0 else "FAILED", result.exit_code, result.output, result.session_id, result.failure_class, result.usage)
    if result.usage.get("soft_budget_exceeded"):
        store.add_message("BLOCKER", {"reason": "soft context budget exceeded", "input_tokens": result.usage.get("input_tokens")}, task_id=task_id, worker_id=worker_id)
    _record_run_evidence(store, run.run_id, result, profile, run.cwd, run.adapter, "managed run")
    store.set_task_status(task_id, status)
    store.release_task_leases(task_id)
    store.add_message("TASK_COMPLETE" if result.exit_code == 0 else "BLOCKER",
                      {"exit_code": result.exit_code, "output": result.output}, task_id, worker_id)
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


def resume_managed_worker(store: Store, task_id: str, worker_id: str, adapter: ProviderAdapter,
                          session_id: str, prompt: str, cwd: str | Path, profile: ExecutionProfile | None = None) -> ManagedRun:
    task = store.task(task_id)
    if task is None: raise ValueError(f"unknown task: {task_id}")
    previous = store.latest_run(task_id, worker_id)
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
    resume_snapshot = from_snapshot or profile.snapshot() == snapshot.snapshot()
    try:
        run = adapter.resume(session_id, prompt, Path(cwd), profile, resume_snapshot=resume_snapshot)
    except TypeError as error:
        if "positional argument" not in str(error) and "unexpected keyword" not in str(error): raise
        try:
            run = adapter.resume(session_id, prompt, Path(cwd), profile)
        except TypeError as legacy_error:
            if "positional argument" not in str(legacy_error): raise
            run = adapter.resume(session_id, prompt, Path(cwd))
    run.run_id = f"run-{uuid.uuid4().hex}"
    store.start_run(run.run_id, task_id, worker_id, adapter.name, run.session_id, profile)
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
