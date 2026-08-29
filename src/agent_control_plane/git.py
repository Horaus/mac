from __future__ import annotations

import subprocess
from pathlib import Path


class GitIntegration:
    """Supervisor-owned Git operations; workers are never merged by this adapter implicitly."""

    def __init__(self, project: str | Path):
        self.project = Path(project)

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

    def create_worktree(self, worktree: str | Path, branch: str, base: str = "HEAD") -> None:
        # Explicit operation: caller must later inspect and accept the result.
        self._run("worktree", "add", "-b", branch, str(worktree), base)

    def diff(self, worktree: str | Path) -> str:
        result = subprocess.run(["git", "-C", str(worktree), "diff", "HEAD"], text=True, capture_output=True, check=True)
        return result.stdout

    def commit_worker(self, worktree: str | Path, message: str) -> str:
        self._run("-C", str(worktree), "add", "-A")
        self._run("-C", str(worktree), "-c", "user.name=agent-control-plane", "-c",
                  "user.email=acp@localhost", "commit", "-m", message)
        result = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], text=True, capture_output=True, check=True)
        return result.stdout.strip()

    def accept_commit(self, commit: str, strategy: str = "cherry-pick") -> str:
        if strategy != "cherry-pick":
            raise ValueError("only explicit cherry-pick acceptance is supported in the MVP")
        return self._run("cherry-pick", commit)

    def abort_cherry_pick(self) -> None:
        subprocess.run(["git", "cherry-pick", "--abort"], cwd=self.project, text=True,
                       capture_output=True, check=False)

    def remove_worktree(self, worktree: str | Path, force: bool = False) -> None:
        args = ["worktree", "remove"]
        if force: args.append("--force")
        args.append(str(worktree))
        self._run(*args)
