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


def run_worker(store: Store, task_id: str, worker_id: str, adapter: ProviderAdapter,
               prompt: str, cwd: str | Path, base_commit: str | None = None,
               profile: ExecutionProfile | None = None) -> WorkerResult:
    """Run one provider worker and record a reviewable result; never integrate it."""
    task = store.task(task_id)
    if task is None:
        raise ValueError(f"unknown task: {task_id}")
    store.add_worker(worker_id, adapter.name, worktree=str(cwd))
    profile = profile or ExecutionProfile()
    adapter.validate_profile(profile)
    store.claim_task(task_id, worker_id, base_commit)
    store.capture_declared_resources(task_id)
    prompt = _inject_context(prompt, store.knowledge_context(profile.context.knowledge_mode), profile)
    run_id = f"run-{uuid.uuid4().hex}"
    store.start_run(run_id, task_id, worker_id, adapter.name, profile=profile)
    try:
        try:
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
    status = "REVIEW" if result.exit_code == 0 else "FAILED"
    store.set_worker_status(worker_id, "COMPLETED" if result.exit_code == 0 else "FAILED", result.session_id)
    store.finish_run(run_id, "COMPLETED" if result.exit_code == 0 else "FAILED", result.exit_code, result.output, result.session_id, result.failure_class, result.usage)
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
                         prompt: str, cwd: str | Path, profile: ExecutionProfile | None = None) -> ManagedRun:
    task = store.task(task_id)
    if task is None: raise ValueError(f"unknown task: {task_id}")
    profile = profile or ExecutionProfile()
    adapter.validate_profile(profile)
    store.claim_task(task_id, worker_id)
    prompt = _inject_context(prompt, store.knowledge_context(profile.context.knowledge_mode), profile)
    try:
        run = adapter.start(prompt, Path(cwd), profile)
    except Exception:
        store.set_task_status(task_id, "FAILED")
        raise
    store.add_worker(worker_id, adapter.name, run.session_id, str(cwd))
    run.run_id = f"run-{uuid.uuid4().hex}"
    store.start_run(run.run_id, task_id, worker_id, adapter.name, run.session_id, profile)
    return run


def finish_managed_worker(store: Store, task_id: str, worker_id: str, run: ManagedRun) -> WorkerResult:
    result = run.wait()
    status = "REVIEW" if result.exit_code == 0 else "FAILED"
    store.set_worker_status(worker_id, "COMPLETED" if result.exit_code == 0 else "FAILED", result.session_id)
    store.finish_run(run.run_id, "COMPLETED" if result.exit_code == 0 else "FAILED", result.exit_code, result.output, result.session_id, result.failure_class, result.usage)
    store.set_task_status(task_id, status)
    store.release_task_leases(task_id)
    store.add_message("TASK_COMPLETE" if result.exit_code == 0 else "BLOCKER",
                      {"exit_code": result.exit_code, "output": result.output}, task_id, worker_id)
    return result


def pause_managed_worker(store: Store, task_id: str, worker_id: str, run: ManagedRun,
                         reason: str = "supervisor pause") -> None:
    """Pause the live process and make the durable task state observable."""
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
