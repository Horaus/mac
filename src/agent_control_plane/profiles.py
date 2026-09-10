"""Provider-neutral execution profile validation and resolution."""
from dataclasses import asdict, dataclass, field
from typing import Any

@dataclass(frozen=True)
class ContextPolicy:
    history_limit: int = 6
    knowledge_mode: str = "summary"
    max_input_tokens: int | None = 12000
    allowed_paths: tuple[str, ...] = field(default_factory=tuple)
    max_output_bytes: int = 1_000_000
    hard_input_tokens: int | None = None

@dataclass(frozen=True)
class ExecutionProfile:
    model: str | None = None
    profile: str | None = None
    reasoning_effort: str | None = None
    sandbox: str | None = None
    context: ContextPolicy = field(default_factory=ContextPolicy)
    def snapshot(self):
        result = asdict(self); result["context"]["allowed_paths"] = list(self.context.allowed_paths); return result

def validate_profile(profile: ExecutionProfile) -> None:
    if profile.model is not None and (not isinstance(profile.model, str) or not profile.model.strip()): raise ValueError("invalid model")
    if profile.profile is not None and (not isinstance(profile.profile, str) or not profile.profile.strip()): raise ValueError("invalid provider profile")
    if profile.reasoning_effort not in (None, "low", "medium", "high"): raise ValueError("invalid reasoning_effort")
    if profile.sandbox not in (None, "read-only", "workspace-write", "danger-full-access"): raise ValueError("invalid sandbox")
    if profile.context.history_limit < 0 or (profile.context.max_input_tokens is not None and profile.context.max_input_tokens <= 0) or profile.context.max_output_bytes <= 0 or (profile.context.hard_input_tokens is not None and profile.context.hard_input_tokens <= 0): raise ValueError("invalid context budget")
    if profile.context.knowledge_mode not in ("none", "summary", "full"): raise ValueError("invalid knowledge_mode")

def profile_from(value: dict[str, Any] | None) -> ExecutionProfile:
    value = value or {}; context = value.get("context") or {}
    profile = ExecutionProfile(value.get("model"), value.get("profile"), value.get("reasoning_effort"), value.get("sandbox"), ContextPolicy(int(context.get("history_limit", 6)), context.get("knowledge_mode", "summary"), context.get("max_input_tokens", 12000), tuple(context.get("allowed_paths", ())), int(context.get("max_output_bytes", 1_000_000)), context.get("hard_input_tokens")))
    validate_profile(profile); return profile

def resolve_profile(project_default=None, worker=None, task_override=None, dispatch_override=None):
    merged = {}; context = {}
    for value in (project_default or {}, (worker or {}).get("execution", {}), task_override or {}, dispatch_override or {}):
        merged.update({k: v for k, v in value.items() if k != "context"}); context.update(value.get("context", {}))
    merged["context"] = context; return profile_from(merged)

def audit_profile(model: str | None = None) -> ExecutionProfile:
    """Small read/audit preset with bounded context and moderate reasoning."""
    return ExecutionProfile(model=model, reasoning_effort="medium", sandbox="read-only",
                            context=ContextPolicy(history_limit=3, knowledge_mode="summary",
                                                  max_input_tokens=6000))
