from __future__ import annotations

import subprocess
import os
import signal
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


def _provider_env() -> dict[str, str]:
    """Provide a deterministic non-MCP stdin and system command PATH."""
    env = os.environ.copy()
    path = env.get("PATH", "").split(os.pathsep)
    for directory in ("/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        if directory not in path and os.path.isdir(directory):
            path.append(directory)
    env["PATH"] = os.pathsep.join(path)
    return env


@dataclass(frozen=True)
class WorkerResult:
    exit_code: int
    output: str
    session_id: str | None = None


class ProviderAdapter:
    name = "abstract"

    def run(self, prompt: str, cwd: Path) -> WorkerResult:
        raise NotImplementedError

    def command(self, prompt: str) -> list[str]:
        raise NotImplementedError

    def start(self, prompt: str, cwd: Path) -> "ManagedRun":
        return ManagedRun(subprocess.Popen(self.command(prompt), cwd=cwd, text=True,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           stdin=subprocess.DEVNULL, env=_provider_env(), start_new_session=True), self)

    def resume(self, session_id: str, prompt: str, cwd: Path) -> "ManagedRun":
        raise NotImplementedError(f"{self.name} does not support session resume")


class ManagedRun:
    def __init__(self, process: subprocess.Popen, adapter: ProviderAdapter):
        self.process = process
        self.adapter = adapter
        self.session_id = f"pid:{process.pid}"

    def pause(self) -> None:
        if self.process.poll() is None: self._signal(signal.SIGSTOP)

    def resume(self) -> None:
        if self.process.poll() is None: self._signal(signal.SIGCONT)

    def cancel(self) -> None:
        if self.process.poll() is None: self._signal(signal.SIGTERM)

    def _signal(self, signum: int) -> None:
        try:
            os.killpg(os.getpgid(self.process.pid), signum)
        except ProcessLookupError:
            pass

    def wait(self, timeout: float | None = None) -> WorkerResult:
        try:
            output, _ = self.process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._signal(signal.SIGKILL)
            output, _ = self.process.communicate()
        text_output = output or ""
        for line in text_output.splitlines():
            try:
                event = json.loads(line)
                if event.get("type") == "thread.started": self.session_id = event.get("thread_id", self.session_id)
            except json.JSONDecodeError:
                pass
        return WorkerResult(self.process.returncode if self.process.returncode is not None else 0,
                            text_output, self.session_id)


class CodexAdapter(ProviderAdapter):
    """Small adapter; scheduling and acceptance remain provider-independent."""

    name = "codex"

    def __init__(self, executable: str = "codex", extra_args: Sequence[str] = (), timeout: float | None = None):
        self.executable = executable
        self.extra_args = tuple(extra_args)
        self.timeout = timeout

    def run(self, prompt: str, cwd: Path) -> WorkerResult:
        exit_code, output = _run_provider_command(self.command(prompt), cwd, self.timeout)
        if exit_code == 124:
            return WorkerResult(124, output + "\nprovider timeout\n")
        session_id = None
        for line in output.splitlines():
            try:
                event = json.loads(line)
                if event.get("type") == "thread.started": session_id = event.get("thread_id")
            except json.JSONDecodeError:
                continue
        return WorkerResult(exit_code, output, session_id)

    def command(self, prompt: str) -> list[str]:
        return [self.executable, "exec", "--json", "--skip-git-repo-check", *self.extra_args, prompt]

    def resume(self, session_id: str, prompt: str, cwd: Path) -> ManagedRun:
        # `codex exec resume` has a narrower option surface than `exec`; keep
        # the resume invocation limited to stable, supported flags.
        command = [self.executable, "exec", "resume", "--json", "--skip-git-repo-check", session_id, prompt]
        return ManagedRun(subprocess.Popen(command, cwd=cwd, text=True, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=_provider_env(), start_new_session=True), self)


class CommandAdapter(ProviderAdapter):
    """Provider-neutral adapter for any deterministic CLI command."""

    def __init__(self, name: str, executable: str, args=(), timeout: float | None = None):
        self.name, self.executable, self.args, self.timeout = name, executable, tuple(args), timeout

    def command(self, prompt: str) -> list[str]:
        if "{prompt}" in self.args:
            return [self.executable, *(prompt if arg == "{prompt}" else arg for arg in self.args)]
        return [self.executable, *self.args, prompt]

    def run(self, prompt: str, cwd: Path) -> WorkerResult:
        exit_code, output = _run_provider_command(self.command(prompt), cwd, self.timeout)
        if exit_code == 124:
            output += "\nprovider timeout\n"
        return WorkerResult(exit_code, output)


class GeminiAdapter(CommandAdapter):
    """Gemini CLI adapter; auth and eligibility errors remain provider output."""

    def __init__(self, executable: str = "gemini"):
        super().__init__("gemini", executable, ("-p", "{prompt}", "--approval-mode", "yolo", "-o", "json"))


def provider(name: str) -> ProviderAdapter:
    if name == "codex": return CodexAdapter()
    if name == "gemini": return GeminiAdapter()
    raise ValueError(f"unsupported provider: {name}")


def _timeout_output(error: subprocess.TimeoutExpired) -> str:
    """Normalize timeout output across Python/platform combinations."""
    chunks = []
    for chunk in (error.stdout, error.stderr):
        if chunk:
            chunks.append(chunk.decode(errors="replace") if isinstance(chunk, bytes) else chunk)
    return "".join(chunks)


def _run_provider_command(command: list[str], cwd: Path, timeout: float | None) -> tuple[int, str]:
    process = subprocess.Popen(command, cwd=cwd, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env=_provider_env(), start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return (process.returncode if process.returncode is not None else 0, stdout + stderr)
    except subprocess.TimeoutExpired as error:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        output = _timeout_output(error) + stdout + stderr
        return 124, output
