from __future__ import annotations

from pathlib import Path

from .git import GitIntegration
from .providers import ProviderAdapter, WorkerResult
from .service import run_worker, validate
from .store import Store


class Supervisor:
    """High-level, supervisor-owned lifecycle for one implementation run."""

    def __init__(self, project: str | Path, store: Store | None = None):
        self.project = Path(project)
        self.store = store or Store(self.project / ".agent-control-plane" / "state.sqlite3")
        self.git = GitIntegration(self.project)

    def dispatch(self, task_id: str, worker_id: str, adapter: ProviderAdapter, prompt: str,
                 branch: str | None = None, worktree: str | Path | None = None) -> WorkerResult:
        task = self.store.task(task_id)
        if task is None: raise ValueError(f"unknown task: {task_id}")
        branch = branch or f"acp/{task_id}/{worker_id}"
        worktree = Path(worktree or self.project.parent / f"{self.project.name}-{worker_id}")
        base_commit = self.git.current_commit()
        self.git.create_worktree(worktree, branch, base_commit)
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
        self.git.assert_clean_tracked_tree()
        commit = self.git.commit_worker(worktree, commit_message)
        try:
            self.git.accept_commit(commit)
        except Exception as error:
            self.git.abort_cherry_pick()
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

    def discard_worktree(self, worktree: str | Path, force: bool = False) -> None:
        """Explicitly discard a worker checkout; never called implicitly on acceptance."""
        self.git.remove_worktree(worktree, force=force)
