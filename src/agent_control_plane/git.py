from __future__ import annotations

import subprocess
from pathlib import Path


class GitIntegration:
    """Supervisor-owned Git operations; workers are never merged by this adapter implicitly."""

    def __init__(self, project: str | Path, authorize=None):
        self.project = Path(project)
        self.authorize = authorize

    def _require(self, capability: str, payload=None, authorizer=None) -> None:
        gate = authorizer or self.authorize
        if gate is None:
            raise PermissionError(f"Git mutation requires supervisor authority: {capability}")
        gate(capability, payload or {})

    def _run(self, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=self.project, text=True, capture_output=True, check=True)
        return result.stdout.strip()

    def current_commit(self) -> str:
        return self._run("rev-parse", "HEAD")

    def assert_clean_tracked_tree(self) -> None:
        result = subprocess.run(["git", "diff", "--quiet", "HEAD", "--"], cwd=self.project,
                                text=True, capture_output=True, check=False)
        if result.returncode != 0:
            raise RuntimeError("integration branch has uncommitted tracked changes")

    def create_worktree(self, worktree: str | Path, branch: str, base: str = "HEAD", task_id: str | None = None, authorizer=None) -> None:
        self._require("git.worktree_create", {"worktree": str(worktree), "branch": branch, "base": base, "task_id": task_id}, authorizer)
        self._run("worktree", "add", "-b", branch, str(worktree), base)

    def diff(self, worktree: str | Path) -> str:
        result = subprocess.run(["git", "-C", str(worktree), "diff", "HEAD"], text=True, capture_output=True, check=True)
        return result.stdout

    def commit_worker(self, worktree: str | Path, message: str, task_id: str | None = None) -> str:
        self._require("git.commit", {"worktree": str(worktree), "message": message, "task_id": task_id})
        self._run("-C", str(worktree), "add", "-A")
        self._run("-C", str(worktree), "-c", "user.name=agent-control-plane", "-c",
                  "user.email=acp@localhost", "commit", "-m", message)
        result = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], text=True, capture_output=True, check=True)
        return result.stdout.strip()

    def accept_commit(self, commit: str, strategy: str = "cherry-pick", task_id: str | None = None) -> str:
        self._require("git.integrate", {"commit": commit, "strategy": strategy, "task_id": task_id})
        if strategy != "cherry-pick":
            raise ValueError("only explicit cherry-pick acceptance is supported in the MVP")
        return self._run("cherry-pick", commit)

    def abort_cherry_pick(self, task_id: str | None = None) -> None:
        self._require("git.cherry_pick_abort", {"task_id": task_id})
        subprocess.run(["git", "cherry-pick", "--abort"], cwd=self.project, text=True,
                       capture_output=True, check=False)

    def remove_worktree(self, worktree: str | Path, force: bool = False, task_id: str | None = None) -> None:
        self._require("destructive.delete", {"worktree": str(worktree), "force": force, "task_id": task_id})
        args = ["worktree", "remove"]
        if force: args.append("--force")
        args.append(str(worktree))
        self._run(*args)

    def push(self, remote: str = "origin", branch: str = "HEAD", authorizer=None, *, protected_branch_authorized: bool = False) -> str:
        destination = branch.rsplit(":", 1)[-1]
        resolved = self._run("branch", "--show-current") if destination == "HEAD" else destination.removeprefix("refs/heads/")
        if resolved in {"main", "master"} and not protected_branch_authorized:
            raise PermissionError(f"push to protected branch requires explicit Master authorization: {resolved}")
        self._require("git.push", {"remote": remote, "branch": branch}, authorizer)
        return self._run("push", remote, branch)
