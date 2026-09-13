"""Deterministic workspace isolation resolved before provider dispatch."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ISOLATION_MODES = {"in-place", "persistent-worktree"}
PROTECTED_BRANCHES = {"main", "master"}


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or "git isolation preflight failed")
    return result.stdout.strip()


def prepare_worker_workspace(store, task, worker_id: str, cwd: str | Path,
                             dispatch_isolation: str | None, workspace_write: bool) -> dict:
    execution = json.loads(task["execution_json"] or "{}")
    persisted = execution.get("isolation")
    if dispatch_isolation and persisted and dispatch_isolation != persisted:
        raise PermissionError("dispatch isolation cannot override persisted task isolation")
    requested = dispatch_isolation or persisted or "in-place"
    if requested not in ISOLATION_MODES:
        raise ValueError(f"invalid isolation: {requested}")
    supplied = Path(cwd).resolve()
    effective = supplied
    try:
        root = Path(_git(supplied, "rev-parse", "--show-toplevel")).resolve()
        branch = _git(supplied, "branch", "--show-current")
    except RuntimeError:
        if requested == "persistent-worktree" or workspace_write:
            raise PermissionError("writable isolation requires a Git worktree")
        root, branch = supplied, None
    if requested == "persistent-worktree":
        safe_task = re.sub(r"[^A-Za-z0-9._-]+", "-", task["id"]).strip("-")
        safe_worker = re.sub(r"[^A-Za-z0-9._-]+", "-", worker_id).strip("-")
        branch = f"mac/{safe_task}/{safe_worker}"
        effective = root.parent / f".{root.name}-mac-worktrees" / f"{safe_task}-{safe_worker}"
        effective.parent.mkdir(parents=True, exist_ok=True)
        if not effective.exists():
            exists = subprocess.run(["git", "-C", str(root), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]).returncode == 0
            args = ["worktree", "add", str(effective), branch] if exists else ["worktree", "add", "-b", branch, str(effective), task["base_commit"] or "HEAD"]
            _git(root, *args)
        actual_root = Path(_git(effective, "rev-parse", "--show-toplevel")).resolve()
        actual_branch = _git(effective, "branch", "--show-current")
        if actual_root != effective.resolve() or actual_branch != branch:
            raise PermissionError("persistent worktree identity mismatch")
    if workspace_write and branch in PROTECTED_BRANCHES:
        raise PermissionError(f"workspace-write dispatch denied on protected branch: {branch}")
    return {"requested_isolation": requested, "effective_worktree": str(effective), "effective_branch": branch}
