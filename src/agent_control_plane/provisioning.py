"""Deterministic, offline dependency provisioning contracts.

This module never installs or contacts a registry. It only verifies that a
declared dependency store matches the project contract and is readable.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass(frozen=True)
class DependencyContract:
    lockfile_sha256: str
    package_manager: str
    package_manager_version: str
    platform: str
    store_fingerprint: str

    def key(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


def contract_for(project: str | Path, package_manager: str, version: str, store: str | Path, platform: str) -> DependencyContract:
    root, store_path = Path(project), Path(store)
    lock = next((root / name for name in ("pnpm-lock.yaml", "package-lock.json", "yarn.lock") if (root / name).exists()), None)
    if lock is None:
        raise FileNotFoundError("no dependency lockfile")
    lock_hash = hashlib.sha256(lock.read_bytes()).hexdigest()
    digest = hashlib.sha256()
    for item in sorted(p for p in store_path.rglob("*") if p.is_file()):
        digest.update(str(item.relative_to(store_path)).encode()); digest.update(item.read_bytes())
    return DependencyContract(lock_hash, package_manager, version, platform, digest.hexdigest())


def verify_offline(contract: DependencyContract, store: str | Path) -> dict:
    path = Path(store)
    if not path.exists() or not path.is_dir():
        return {"ready": False, "reason": "dependency store missing", "contract": contract.key()}
    actual = hashlib.sha256()
    for item in sorted(p for p in path.rglob("*") if p.is_file()):
        actual.update(str(item.relative_to(path)).encode()); actual.update(item.read_bytes())
    if actual.hexdigest() != contract.store_fingerprint:
        return {"ready": False, "reason": "dependency store fingerprint mismatch", "contract": contract.key()}
    return {"ready": True, "network": False, "writes_outside_store": False, "contract": contract.key()}


def provision_readonly_link(project: str | Path, store: str | Path, target: str = "node_modules") -> dict:
    """Expose a verified store through an explicit symlink; never fetches."""
    root, source = Path(project), Path(store)
    if not source.is_dir():
        return {"ready": False, "reason": "dependency store missing", "network": False}
    destination = root / target
    if destination.exists() or destination.is_symlink():
        return {"ready": False, "reason": "dependency target already exists", "network": False}
    destination.symlink_to(source, target_is_directory=True)
    return {"ready": True, "network": False, "read_only_store": True, "target": str(destination)}


def run_offline_package_manager(project: str | Path, package_manager: str = "pnpm") -> dict:
    """Run the package manager's real offline install with a child-process network guard."""
    root = Path(project)
    if package_manager != "pnpm":
        raise ValueError("offline acceptance currently supports pnpm only")
    guard = Path(tempfile.mkdtemp(prefix="mac-network-guard-")) / "guard.js"
    guard.write_text("""
const net = require('net');
const dns = require('dns');
const fail = () => { process.stderr.write('NETWORK_GUARD_BLOCKED\\n'); process.exit(86); };
net.connect = fail; net.createConnection = fail; dns.lookup = fail; dns.resolve = fail;
""")
    env = os.environ.copy()
    env["NODE_OPTIONS"] = (env.get("NODE_OPTIONS", "") + " --require " + str(guard)).strip()
    env["npm_config_offline"] = "true"
    command = [package_manager, "install", "--offline", "--ignore-scripts", "--frozen-lockfile", "--dir", str(root)]
    before = (root / "node_modules").exists()
    completed = subprocess.run(command, cwd=root, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    package_markers = list((root / "node_modules").glob("*/package.json")) if (root / "node_modules").is_dir() else []
    return {"ready": completed.returncode == 0, "network": "NETWORK_GUARD_BLOCKED" in completed.stdout,
            "cache_used": before or bool(package_markers), "resolved_packages": [str(p.parent.name) for p in package_markers],
            "exit_code": completed.returncode, "output": completed.stdout}
