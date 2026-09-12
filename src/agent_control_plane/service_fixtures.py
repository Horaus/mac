"""Sandboxed launchd/systemd installation fixtures; never mutate the host."""
from __future__ import annotations
import json
import os
import subprocess
from pathlib import Path


class SandboxedServiceManager:
    def __init__(self, root: str | Path, manager: str):
        if manager not in {"launchd", "systemd"}: raise ValueError("unsupported service manager")
        self.root = Path(root).resolve(); self.manager = manager

    @property
    def manifest_path(self):
        return self.root / ("com.mac.agent-control-plane.json" if self.manager == "launchd" else "mac-agent-control-plane.service.json")

    def install(self, command: list[str], state_path: str | Path) -> dict:
        if not command or not all(isinstance(item, str) and item for item in command): raise ValueError("service command required")
        state = Path(state_path).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        manifest = {"manager": self.manager, "command": command, "state_path": str(state), "restart": "KeepAlive" if self.manager == "launchd" else "always", "fixture": True}
        self.manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n")
        return manifest

    def uninstall(self) -> bool:
        if self.manifest_path.is_file(): self.manifest_path.unlink(); return True
        return False

    def install_unit(self, command: list[str], state_path: str | Path) -> dict:
        """Render the real manager unit format inside an explicit sandbox.

        This never invokes launchctl/systemctl; the returned path is suitable
        for an operator or OS-specific installer to apply externally.
        """
        if not command or not all(isinstance(item, str) and item for item in command):
            raise ValueError("service command required")
        state = str(Path(state_path).resolve()); executable = " ".join(command)
        self.root.mkdir(parents=True, exist_ok=True)
        if self.manager == "launchd":
            path = self.root / "com.mac.agent-control-plane.plist"
            content = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<plist version=\"1.0\"><dict>\n<key>Label</key><string>com.mac.agent-control-plane</string>\n<key>ProgramArguments</key><array>""" + "".join(f"<string>{item}</string>" for item in command) + f"""</array>\n<key>KeepAlive</key><true/>\n<key>EnvironmentVariables</key><dict><key>MAC_STATE</key><string>{state}</string></dict>\n</dict></plist>\n"""
        else:
            path = self.root / "mac-agent-control-plane.service"
            content = f"""[Unit]\nDescription=MAC Agent Control Plane\nAfter=network.target\n[Service]\nExecStart={executable}\nEnvironment=MAC_STATE={state}\nRestart=always\n[Install]\nWantedBy=default.target\n"""
        path.write_text(content)
        return {"manager": self.manager, "path": str(path), "format": "plist" if self.manager == "launchd" else "systemd-unit", "sandboxed": True}

    def uninstall_unit(self) -> bool:
        path = self.root / ("com.mac.agent-control-plane.plist" if self.manager == "launchd" else "mac-agent-control-plane.service")
        if path.is_file(): path.unlink(); return True
        return False


class ProductionServiceManager(SandboxedServiceManager):
    """Manager boundary with injected OS executor; never hard-codes host calls."""
    def __init__(self, root, manager, executor):
        super().__init__(root, manager)
        if not callable(executor): raise ValueError("service executor required")
        self.executor = executor

    def install(self, command, state_path):
        rendered = self.install_unit(command, state_path)
        try:
            self.executor("install", Path(rendered["path"]))
        except Exception:
            self.uninstall_unit()
            raise
        return {**rendered, "status": "installed"}

    def uninstall(self):
        path = self.root / ("com.mac.agent-control-plane.plist" if self.manager == "launchd" else "mac-agent-control-plane.service")
        try:
            self.executor("uninstall", path)
        except Exception:
            raise
        return self.uninstall_unit()

    def status(self):
        path = self.root / ("com.mac.agent-control-plane.plist" if self.manager == "launchd" else "mac-agent-control-plane.service")
        return {"manager": self.manager, "installed": path.exists(), "status": self.executor("status", path), "path": str(path)}


def subprocess_service_executor(manager: str):
    """Build the real host executor; callers/tests may inject another one."""
    if manager not in {"launchd", "systemd"}: raise ValueError("unsupported service manager")
    def execute(action, path):
        if manager == "launchd":
            target = f"gui/{os.getuid()}/com.mac.agent-control-plane"
            command = (["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)] if action == "install" else
                       ["launchctl", "bootout", target] if action == "uninstall" else
                       ["launchctl", "print", target])
        else:
            unit = path.name
            command = (["systemctl", "--user", "enable", "--now", str(path)] if action == "install" else
                       ["systemctl", "--user", "disable", "--now", unit] if action == "uninstall" else
                       ["systemctl", "--user", "is-active", unit])
        result = subprocess.run(command, capture_output=True, text=True, check=action != "status")
        return result.stdout.strip() or result.returncode == 0
    return execute
