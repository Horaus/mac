from __future__ import annotations

import subprocess
import os
import signal
import json
import select
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from .profiles import ExecutionProfile, validate_profile


def _packet_prompt(prompt: str, context_packet) -> str:
    if context_packet is None:
        return prompt
    snapshot = context_packet.snapshot() if hasattr(context_packet, "snapshot") else context_packet
    return prompt + "\n\nSUPERVISOR CONTEXT PACKET:\n" + json.dumps(snapshot, sort_keys=True, default=str)

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
    usage: dict | None = None
    failure_class: str | None = None


def parse_usage(output: str) -> dict:
    """Parse JSONL usage while preserving event-vs-cumulative semantics."""
    totals = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    found = False
    for line in output.splitlines():
        try: event = json.loads(line)
        except json.JSONDecodeError: continue
        usage = event.get("usage") or event.get("token_usage")
        if not isinstance(usage, dict): continue
        found = True
        cumulative = bool(event.get("cumulative") or usage.get("cumulative"))
        for key in totals:
            value = usage.get(key)
            if isinstance(value, int):
                totals[key] = max(totals[key], value) if cumulative else totals[key] + value
    return totals if found else {}


def classify_failure(exit_code: int, output: str) -> str | None:
    text = output.lower()
    if exit_code == 0: return None
    if any(x in text for x in ("unauthorized", "authentication", "not logged in", "api key")): return "AUTH_ERROR"
    if any(x in text for x in ("model not found", "model unavailable", "does not exist", "not entitled")): return "MODEL_UNAVAILABLE"
    if any(x in text for x in ("invalid profile", "invalid argument", "configuration error")): return "CONFIGURATION_ERROR"
    return "PROVIDER_FAILURE"


class ProviderAdapter:
    name = "abstract"

    def validate_profile(self, profile: ExecutionProfile) -> None:
        validate_profile(profile)

    def submit(self, payload: dict):
        raise NotImplementedError(f"{self.name} does not expose submission")

    def run(self, prompt: str, cwd: Path, profile: ExecutionProfile | None = None, context_packet=None) -> WorkerResult:
        raise NotImplementedError

    def command(self, prompt: str, profile: ExecutionProfile | None = None) -> list[str]:
        raise NotImplementedError

    def start(self, prompt: str, cwd: Path, profile: ExecutionProfile | None = None, context_packet=None) -> "ManagedRun":
        self.validate_profile(profile or ExecutionProfile())
        try:
            command = self.command(_packet_prompt(prompt, context_packet), profile)
        except TypeError:
            command = self.command(prompt)
        managed = ManagedRun(subprocess.Popen(command, cwd=cwd, text=True,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           stdin=subprocess.DEVNULL, env=_provider_env(), start_new_session=True), self, cwd)
        managed.hard_input_tokens = getattr(profile.context, "hard_input_tokens", None) if profile else None
        return managed

    def resume(self, session_id: str, prompt: str, cwd: Path) -> "ManagedRun":
        raise NotImplementedError(f"{self.name} does not support session resume")


class ManagedRun:
    def __init__(self, process: subprocess.Popen, adapter: ProviderAdapter, cwd: Path | None = None):
        self.process = process
        self.adapter = adapter
        self.cwd = Path(cwd or ".")
        self.session_id = f"pid:{process.pid}"
        self.hard_input_tokens: int | None = None

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
        if self.hard_input_tokens is not None and self.process.stdout is not None:
            return self._wait_stream(timeout)
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
        exit_code = self.process.returncode if self.process.returncode is not None else 0
        return WorkerResult(exit_code, text_output, self.session_id,
                            parse_usage(text_output), classify_failure(exit_code, text_output))

    def _wait_stream(self, timeout: float | None = None) -> WorkerResult:
        started = __import__("time").monotonic(); lines = []; denied = False
        while self.process.poll() is None:
            if timeout is not None and __import__("time").monotonic() - started > timeout:
                self._signal(signal.SIGKILL); break
            ready, _, _ = select.select([self.process.stdout], [], [], 0.05)
            if not ready: continue
            line = self.process.stdout.readline()
            if not line: continue
            lines.append(line)
            usage = parse_usage(line)
            if usage.get("input_tokens", 0) > self.hard_input_tokens:
                denied = True; self._signal(signal.SIGKILL); break
        tail, _ = self.process.communicate()
        if tail: lines.append(tail)
        output = "".join(lines)
        if denied: output += "\nhard cumulative input budget exceeded\n"
        exit_code = self.process.returncode if self.process.returncode is not None else 137
        return WorkerResult(exit_code, output, self.session_id, parse_usage(output), "hard_budget_denied" if denied else classify_failure(exit_code, output))


class CodexAdapter(ProviderAdapter):
    """Small adapter; scheduling and acceptance remain provider-independent."""

    name = "codex"

    def __init__(self, executable: str = "codex", extra_args: Sequence[str] = (), timeout: float | None = None):
        self.executable = executable
        self.extra_args = tuple(extra_args)
        self.timeout = timeout

    def run(self, prompt: str, cwd: Path, profile: ExecutionProfile | None = None, context_packet=None) -> WorkerResult:
        self.validate_profile(profile or ExecutionProfile())
        exit_code, output = _run_provider_command(self.command(_packet_prompt(prompt, context_packet), profile), cwd, self.timeout)
        if exit_code == 124:
            return WorkerResult(124, output + "\nprovider timeout\n")
        session_id = None
        for line in output.splitlines():
            try:
                event = json.loads(line)
                if event.get("type") == "thread.started": session_id = event.get("thread_id")
            except json.JSONDecodeError:
                continue
        return WorkerResult(exit_code, output, session_id, parse_usage(output), classify_failure(exit_code, output))

    def command(self, prompt: str, profile: ExecutionProfile | None = None) -> list[str]:
        profile = profile or ExecutionProfile()
        command = [self.executable, "exec", "--json", "--skip-git-repo-check"]
        if profile.model: command += ["--model", profile.model]
        if profile.profile: command += ["--profile", profile.profile]
        if profile.sandbox: command += ["--sandbox", profile.sandbox]
        if profile.reasoning_effort: command += ["-c", f'model_reasoning_effort="{profile.reasoning_effort}"']
        return [*command, *self.extra_args, prompt]

    def resume(self, session_id: str, prompt: str, cwd: Path, profile: ExecutionProfile | None = None,
               resume_snapshot: bool = False) -> ManagedRun:
        # `codex exec resume` has a narrower option surface than `exec`; keep
        # the resume invocation limited to stable, supported flags.
        command = self.resume_command(session_id, prompt, profile, resume_snapshot=resume_snapshot)
        return ManagedRun(subprocess.Popen(command, cwd=cwd, text=True, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=_provider_env(), start_new_session=True), self)

    def resume_command(self, session_id: str, prompt: str, profile: ExecutionProfile | None = None,
                       resume_snapshot: bool = False) -> list[str]:
        """Build only flags accepted by the installed `exec resume` surface."""
        profile = profile or ExecutionProfile()
        if not resume_snapshot: self.validate_resume_profile(profile)
        command = [self.executable, "exec", "resume", "--json", "--skip-git-repo-check"]
        if profile.model: command += ["--model", profile.model]
        if profile.reasoning_effort: command += ["-c", f'model_reasoning_effort="{profile.reasoning_effort}"']
        return command + [session_id, prompt]

    @staticmethod
    def validate_resume_profile(profile: ExecutionProfile) -> None:
        validate_profile(profile)
        if profile.profile is not None:
            raise ValueError("Codex exec resume does not support --profile; reconfigure the session explicitly")
        if profile.sandbox is not None:
            raise ValueError("Codex exec resume does not support --sandbox reconfiguration")


class CommandAdapter(ProviderAdapter):
    """Provider-neutral adapter for any deterministic CLI command."""

    def __init__(self, name: str, executable: str, args=(), timeout: float | None = None):
        self.name, self.executable, self.args, self.timeout = name, executable, tuple(args), timeout

    def validate_profile(self, profile: ExecutionProfile) -> None:
        validate_profile(profile)
        unsupported = []
        if profile.model is not None: unsupported.append("model")
        if profile.profile is not None: unsupported.append("profile")
        if profile.reasoning_effort is not None: unsupported.append("reasoning_effort")
        if profile.sandbox is not None: unsupported.append("sandbox")
        if unsupported:
            raise ValueError(f"{self.name} adapter does not support execution fields: {', '.join(unsupported)}")

    def command(self, prompt: str, profile: ExecutionProfile | None = None) -> list[str]:
        if "{prompt}" in self.args:
            return [self.executable, *(prompt if arg == "{prompt}" else arg for arg in self.args)]
        return [self.executable, *self.args, prompt]

    def run(self, prompt: str, cwd: Path, profile: ExecutionProfile | None = None, context_packet=None) -> WorkerResult:
        self.validate_profile(profile or ExecutionProfile())
        exit_code, output = _run_provider_command(self.command(_packet_prompt(prompt, context_packet), profile), cwd, self.timeout)
        if exit_code == 124:
            output += "\nprovider timeout\n"
        return WorkerResult(exit_code, output, usage=parse_usage(output), failure_class=classify_failure(exit_code, output))


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
