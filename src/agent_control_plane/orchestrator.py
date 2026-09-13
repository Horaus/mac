from __future__ import annotations

from pathlib import Path
import json

from .git import GitIntegration
from .providers import ProviderAdapter, WorkerResult
from .service import run_worker, validate
from .store import Store
from .authority import AuthorityPolicy, digest
from .service import authorize_action


class Supervisor:
    """High-level, supervisor-owned lifecycle for one implementation run."""

    def __init__(self, project: str | Path, store: Store | None = None):
        self.project = Path(project)
        self.store = store or Store(self.project / ".agent-control-plane" / "state.sqlite3")
        self.git = GitIntegration(self.project, self._authorize_git)
        self.control_policy = AuthorityPolicy(capabilities=frozenset({"git.worktree_create", "git.commit", "git.integrate", "git.cherry_pick_abort", "git.push", "destructive.delete"}))

    def _authorize_git(self, capability: str, payload: dict) -> None:
        task_id = payload.get("task_id")
        run = self.store.latest_run(task_id) if task_id else None
        if not run:
            raise PermissionError("Git mutation requires an authority-managed run")
        self.control_policy.require(capability)

    def dispatch(self, task_id: str, worker_id: str, adapter: ProviderAdapter, prompt: str,
                 branch: str | None = None, worktree: str | Path | None = None) -> WorkerResult:
        task = self.store.task(task_id)
        if task is None: raise ValueError(f"unknown task: {task_id}")
        branch = branch or f"acp/{task_id}/{worker_id}"
        worktree = Path(worktree or self.project.parent / f"{self.project.name}-{worker_id}")
        base_commit = self.git.current_commit()
        def dispatch_gate(capability, payload):
            self.control_policy.require(capability)
        self.git.create_worktree(worktree, branch, base_commit, task_id, dispatch_gate)
        result = run_worker(self.store, task_id, worker_id, adapter, prompt, worktree, base_commit)
        return result

    def accept(self, task_id: str, worktree: str | Path, commit_message: str,
               validation: tuple[str, ...], validation_timeout: float = 300) -> str:
        task = self.store.task(task_id)
        if task is None or task["status"] != "REVIEW":
            raise ValueError("only REVIEW tasks can be accepted")
        if not validation:
            raise ValueError("at least one validation command is required before acceptance")
        for command in validation:
            if validate(self.store, task_id, command, worktree, validation_timeout) != 0:
                self.store.reject_task(task_id, f"validation failed: {command}")
                raise RuntimeError(f"validation failed: {command}")
        latest = self.store.latest_run(task_id)
        if latest and self.store.authority_snapshot(latest["id"]):
            # These are captured from supervisor-owned observations before any
            # commit/cherry-pick side effect. No prompt or worker claim supplies them.
            diff = self.git.diff(worktree)
            self.store._emit_diff_observed(latest["id"], {"sha256": __import__("hashlib").sha256(diff.encode()).hexdigest(), "bytes": len(diff)})
            self.store._emit_integration_preflight(latest["id"], {"commands": list(validation)})
            leases = [dict(row) for row in self.store.db.execute("SELECT resource,mode,worker_id FROM leases WHERE task_id=?", (task_id,))]
            self.store._emit_mutation_preflight(latest["id"], {"leases": leases})
            missing = self.store.review_evidence(task_id)
            kinds = {event["kind"] for event in self.store.authority_events(latest["id"], task_id)}
            missing += [kind + " event" for kind in ("diff_observed", "integration_preflight", "mutation_preflight") if kind not in kinds]
            if missing:
                raise ValueError("review evidence incomplete before integration: " + ", ".join(missing))
        self.git.assert_clean_tracked_tree()
        commit = self.git.commit_worker(worktree, commit_message, task_id)
        try:
            self.git.accept_commit(commit, task_id=task_id)
        except Exception as error:
            self.git.abort_cherry_pick(task_id)
            self.store.set_task_status(task_id, "DISPUTED")
            self.store.add_message("CONFLICT_REPORT", {"commit": commit, "error": str(error)}, task_id=task_id)
            raise
        self.store.accept_task(task_id)
        self.store.record_integration(task_id, commit)
        # A successful write integration publishes new logical contract
        # versions.  The store then invalidates only tasks that consumed the
        # affected resources; unrelated workers remain runnable.
        for resource in self.store.db.execute(
                "SELECT resource FROM task_resources WHERE task_id=? AND mode='WRITE'",
                (task_id,)).fetchall():
            self.store.bump_resource(resource["resource"])
        return commit

    def close(self) -> None:
        self.store.close()

    def reconcile(self) -> list[str]:
        return self.store.reconcile()

    def discard_worktree(self, worktree: str | Path, task_id: str, force: bool = False) -> None:
        """Explicitly discard a worker checkout; never called implicitly on acceptance."""
        self.git.remove_worktree(worktree, force=force, task_id=task_id)

    def push(self, remote: str, branch: str, policy, token_id: str, project_id: str,
             provider_id: str, job_id: str, idempotency_key: str, run_id: str, task_id: str) -> str:
        """Explicit supervisor push boundary; caller must provide scoped approval."""
        payload = {"remote": remote, "branch": branch}
        authorize_action(self.store, policy, "git.push", payload, token_id=token_id,
                         project_id=project_id, provider_id=provider_id, job_id=job_id,
                         idempotency_key=idempotency_key, run_id=run_id, task_id=task_id)
        def push_gate(capability, ignored_payload):
            if capability != "git.push": raise PermissionError("invalid Git operation")
            self.control_policy.require(capability)
        return self.git.push(remote, branch, push_gate, protected_branch_authorized=True)
