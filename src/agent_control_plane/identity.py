"""Read-only installation identity and doctor mismatch checks."""
from __future__ import annotations

import subprocess
import sys
import importlib.metadata
import shutil
from pathlib import Path


def installation_identity(root: str | Path) -> dict:
    root = Path(root)
    try:
        revision = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        revision = None
    try:
        source = subprocess.check_output(["git", "-C", str(root), "config", "--get", "remote.origin.url"], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        source = None
    try:
        dirty = bool(subprocess.check_output(["git", "-C", str(root), "status", "--porcelain"], text=True, stderr=subprocess.DEVNULL).strip())
    except (OSError, subprocess.CalledProcessError):
        dirty = None
    package_version = None
    try:
        for line in (root / "pyproject.toml").read_text().splitlines():
            if line.strip().startswith("version") and "=" in line:
                package_version = line.split("=", 1)[1].strip().strip('"\'')
    except OSError:
        try:
            from . import __version__
            package_version = __version__
        except ImportError:
            try: package_version = importlib.metadata.version("agent-control-plane")
            except importlib.metadata.PackageNotFoundError: package_version = None
    launcher = root / "scripts" / "agent-control-plane"
    if not launcher.exists():
        launcher = Path(shutil.which("acp") or sys.executable)
    return {"root": str(root.resolve()), "source": source, "revision": revision, "dirty": dirty,
            "package_version": package_version,
            "launcher": str(launcher.resolve()),
            "state": str((root / ".agent-control-plane" / "state.sqlite3").resolve()),
            "mcp_command": str(Path(sys.executable).resolve()),
            "mcp_args": ["-m", "agent_control_plane", "mcp", "--state", str((root / ".agent-control-plane" / "state.sqlite3").resolve())]}


def identity_mismatches(actual: dict, expected: dict) -> list[str]:
    return [key for key, value in expected.items() if value is not None and actual.get(key) != value]
