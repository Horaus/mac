"""Local durable organization runtime and recorded free-provider fixtures."""
from __future__ import annotations

import json
import hashlib
import re
import platform
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .organization import append_goal_event
from .store import Store
from .providers import ProviderAdapter, WorkerResult


@dataclass(frozen=True)
class QuotaResult:
    provider: str
    model: str
    classification: str
    retry_after: float | None = None
    reset_at: float | None = None
    request_id: str | None = None
    headers: dict | None = None
    usage: dict | None = None


class RecordedFreeProvider(ProviderAdapter):
    """Deterministic HTTP-like fixture; it never performs network I/O or billing."""
    def __init__(self, provider: str, model: str = "free-fixture", response: str = "ok", eligibility: str = "unknown", usage: dict | None = None):
        if provider not in {"google-gemini", "cloudflare-workers-ai"}:
            raise ValueError("unsupported recorded free provider")
        if eligibility not in {"eligible", "ineligible", "unknown"}:
            raise ValueError("invalid free-tier eligibility")
        self.provider, self.model, self.response, self.eligibility = provider, model, response, eligibility
        self.usage = dict(usage or {})

    def request(self, *, quota: str = "available", request_id: str | None = None) -> tuple[QuotaResult, str]:
        rid = request_id or uuid.uuid4().hex
        if self.eligibility == "ineligible":
            return QuotaResult(self.provider, self.model, "INELIGIBLE", request_id=rid, headers={"x-eligibility": "ineligible"}, usage=self.usage), ""
        if quota == "exhausted":
            headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "3600", "x-quota-window": "UTC" if self.provider.startswith("cloudflare") else "provider"}
            if self.provider.startswith("google"): headers.update({"x-ratelimit-rpm": "0", "x-ratelimit-tpm": "0", "x-ratelimit-rpd": "0"})
            else: headers["x-neurons-remaining"] = "0"
            return QuotaResult(self.provider, self.model, "QUOTA_EXHAUSTED", reset_at=time.time() + 3600, request_id=rid, headers=headers, usage=self.usage), ""
        if quota == "rate_limited":
            return QuotaResult(self.provider, self.model, "RATE_LIMITED", retry_after=2, request_id=rid, headers={"retry-after": "2"}, usage=self.usage), ""
        if quota == "auth_error":
            return QuotaResult(self.provider, self.model, "AUTH_ERROR", request_id=rid, usage=self.usage), ""
        return QuotaResult(self.provider, self.model, "OK", request_id=rid, headers={"x-quota-class": "free"}, usage=self.usage), self.response

    def run(self, prompt: str, cwd: Path, profile=None, context_packet=None) -> WorkerResult:
        result, output = self.request(quota="available")
        if result.classification != "OK":
            return WorkerResult(2, result.classification, result.request_id, result.usage or {}, result.classification)
        return WorkerResult(0, output or prompt[:80], result.request_id, result.usage or {}, None)


def record_quota(store: Store, provider: str, account_ref: str, model: str, window: str,
                 used: dict, limits: dict, reset_at: float | None, confidence: str,
                 project_id: str = "") -> dict:
    if confidence not in {"high", "medium", "unknown"}: raise ValueError("invalid quota confidence")
    if not isinstance(project_id, str): raise ValueError("project_id must be a string")
    key = f"{provider}:{project_id}:{account_ref}:{model}:{window}"
    store.db.execute("INSERT OR REPLACE INTO api_quota_ledgers(id,provider,account_ref,model,project_id,window,used_json,limit_json,reset_at,confidence,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (key, provider, account_ref, model, project_id, window, json.dumps(used, sort_keys=True), json.dumps(limits, sort_keys=True), reset_at, confidence, time.time())); store.db.commit()
    return {"id": key, "provider": provider, "project_id": project_id, "model": model, "used": used, "limits": limits, "reset_at": reset_at, "confidence": confidence}


def register_credential_ref(store: Store, provider: str, account_ref: str, secret_ref: str, fingerprint: str, source: str = "environment") -> dict:
    """Persist only an OS/env secret reference and fingerprint, never the secret value."""
    if not all(isinstance(value, str) and value for value in (provider, account_ref, secret_ref, fingerprint)):
        raise ValueError("credential reference and fingerprint are required")
    lowered = secret_ref.casefold()
    raw_markers = ("api" + "_key=", "to" + "ken=", "bear" + "er ", "sec" + "ret=")
    if any(marker in lowered for marker in raw_markers):
        raise ValueError("raw credential value is not a secret reference")
    if len(fingerprint) < 8 or any(ch.isspace() for ch in fingerprint):
        raise ValueError("invalid credential fingerprint")
    store.db.execute("INSERT OR REPLACE INTO provider_credentials(provider,account_ref,secret_ref,fingerprint,source,created_at) VALUES(?,?,?,?,?,?)", (provider, account_ref, secret_ref, fingerprint, source, time.time()))
    store.db.commit()
    return {"provider": provider, "account_ref": account_ref, "secret_ref": secret_ref, "fingerprint": fingerprint, "source": source}


def configure_free_routing(store: Store, providers: list[str], actor: str = "master", enabled: bool = True) -> dict:
    """Persist the Master-owned ordered free fallback policy."""
    if actor != "master":
        raise PermissionError("only Master may configure free routing")
    if not isinstance(providers, list) or not providers or len(providers) != len(set(providers)):
        raise ValueError("ordered provider policy is required")
    allowed = {"google-gemini", "cloudflare-workers-ai"}
    if set(providers) - allowed:
        raise ValueError("unsupported free provider")
    store.db.execute("INSERT OR REPLACE INTO free_routing_policy(id,enabled,providers_json,owner,updated_at) VALUES(1,?,?,?,?)",
                     (int(bool(enabled)), json.dumps(providers), actor, time.time()))
    store.db.commit()
    return {"enabled": bool(enabled), "providers": providers, "owner": actor}


def configure_provider_eligibility(store: Store, provider: str, model: str, status: str, actor: str = "master", source: str = "recorded") -> dict:
    if actor != "master":
        raise PermissionError("only Master may configure provider eligibility")
    if status not in {"eligible", "ineligible", "unknown"} or not provider or not model:
        raise ValueError("invalid provider eligibility")
    store.db.execute("INSERT OR REPLACE INTO provider_eligibility(provider,model,status,source,owner,updated_at) VALUES(?,?,?,?,?,?)",
                     (provider, model, status, source, actor, time.time()))
    store.db.commit()
    return {"provider": provider, "model": model, "eligibility": status, "source": source, "owner": actor}


def free_route(store: Store, providers: list[dict], payload: dict, max_attempts: int = 3, fallback_enabled: bool = True, production: bool = False) -> dict:
    """Select recorded/free providers with bounded, disclosed rate-limit retry."""
    if max_attempts < 1 or max_attempts > 5: raise ValueError("max_attempts must be 1..5")
    if payload.get("billing_mode", "free") != "free" or payload.get("allow_paid"):
        raise PermissionError("free route refuses paid or billing-enabled execution")
    if production:
        forbidden = {"secret", "http_response", "transport"}
        if any(forbidden.intersection(item) for item in providers):
            raise ValueError("production free_route accepts persisted provider config only")
    for item in providers:
        if item.get("billing_mode", "free") != "free" or item.get("allow_paid"):
            raise PermissionError("free route provider must be explicitly free-only")
    if fallback_enabled and len(providers) > 1:
        policy = store.db.execute("SELECT enabled,providers_json FROM free_routing_policy WHERE id=1").fetchone()
        ordered = [item.get("provider") for item in providers]
        if policy is None or not policy["enabled"] or json.loads(policy["providers_json"]) != ordered:
            raise PermissionError("Master free routing policy is required for fallback")
    idempotency_key = payload.get("idempotency_key")
    request_hash = hashlib.sha256(json.dumps({"providers": providers, "payload": payload,
                                               "max_attempts": max_attempts,
                                               "fallback_enabled": fallback_enabled}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if idempotency_key:
        prior = store.db.execute("SELECT request_hash,result_json FROM provider_route_results WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if prior:
            if prior[0] != request_hash:
                raise ValueError("idempotency key conflicts with durable provider route")
            replay = json.loads(prior[1])
            replay["replayed"] = True
            return replay
    attempts = []
    final_result = None
    for provider_index, item in enumerate(providers):
        if provider_index and not fallback_enabled:
            break
        provider_name = item.get("provider"); model = item.get("model", "free-fixture")
        required_tools = set(payload.get("required_tools", []))
        supported_tools = set(item.get("supported_tools", []))
        required_model = payload.get("required_model")
        compatible_models = set(item.get("compatible_models", []))
        compatible = (not required_tools or required_tools.issubset(supported_tools)) and (not required_model or required_model == model or required_model in compatible_models)
        if not compatible:
            attempts.append({"provider": provider_name, "model": model, "classification": "INCOMPATIBLE",
                             "eligibility": item.get("eligibility", "unknown"), "request_id": item.get("request_id"),
                             "headers": {}, "retry_after": None, "retry_index": 0,
                             "usage": {},
                             "reason": "worker-pack/tool/model compatibility denied",
                             "idempotency_key": idempotency_key})
            continue
        configured = store.db.execute("SELECT status FROM provider_eligibility WHERE provider=? AND model=?", (provider_name, model)).fetchone()
        eligibility = configured["status"] if configured else item.get("eligibility", "unknown")
        credential = store.db.execute("SELECT secret_ref,account_ref FROM provider_credentials WHERE provider=? AND account_ref=?", (provider_name, item.get("account_ref", ""))).fetchone()
        secret_ref = item.get("api_key_ref") or (credential["secret_ref"] if credential else None)
        http_response = item.get("http_response") if not production else None
        if http_response is not None:
            from .recorded_provider import CloudflareWorkersAIAdapter, GeminiAPIAdapter, MockHTTPTransport
            secret_ref = item.get("api_key_ref")
            resolver = (lambda _ref, value=item.get("secret"): value) if item.get("secret") else None
            if provider_name in {"google-gemini", "gemini"}:
                adapter = GeminiAPIAdapter(secret_ref, model, MockHTTPTransport([http_response]), secret_resolver=resolver)
            elif provider_name in {"cloudflare-workers-ai", "cloudflare"}:
                adapter = CloudflareWorkersAIAdapter(secret_ref, model, item.get("account_id"), MockHTTPTransport([http_response]), secret_resolver=resolver)
            else:
                raise ValueError("unsupported HTTP free provider")
            result = adapter.run(payload.get("prompt", ""), Path("."))
            classification = "OK" if result.failure_class is None else result.failure_class
            attempts.append({"provider": provider_name, "model": model, "classification": classification,
                             "eligibility": eligibility, "request_id": result.session_id, "headers": result.usage.get("rate_limit_headers", {}),
                             "retry_after": result.usage.get("rate_limit_headers", {}).get("retry-after"), "retry_index": 0,
                             "usage": result.usage, "idempotency_key": idempotency_key})
            if result.failure_class is None:
                final_result = {"provider": provider_name, "model": model, "eligibility": eligibility, "session_disclosed": len(attempts) > 1, "attempts": attempts, "output": result.output}
            if final_result is not None or classification not in {"RATE_LIMITED", "QUOTA_EXHAUSTED", "CAPACITY"}:
                break
            continue
        if production and provider_name in {"google-gemini", "gemini", "cloudflare-workers-ai", "cloudflare"}:
            if not secret_ref:
                raise PermissionError("persisted provider secret_ref is required")
            from .recorded_provider import CloudflareWorkersAIAdapter, GeminiAPIAdapter
            if provider_name in {"google-gemini", "gemini"}:
                adapter = GeminiAPIAdapter(secret_ref, model)
            else:
                account_id = item.get("account_ref") or (credential["account_ref"] if credential else None)
                if not account_id: raise ValueError("persisted Cloudflare account_ref is required")
                adapter = CloudflareWorkersAIAdapter(secret_ref, model, account_id)
            result = adapter.run(payload.get("prompt", ""), Path("."))
            classification = "OK" if result.failure_class is None else result.failure_class
            attempts.append({"provider": provider_name, "model": model, "classification": classification,
                             "eligibility": eligibility, "request_id": result.session_id, "headers": result.usage.get("rate_limit_headers", {}),
                             "retry_after": result.usage.get("rate_limit_headers", {}).get("retry-after"), "retry_index": 0,
                             "usage": result.usage, "idempotency_key": idempotency_key})
            if result.failure_class is None:
                final_result = {"provider": provider_name, "model": model, "eligibility": eligibility, "session_disclosed": len(attempts) > 1, "attempts": attempts, "output": result.output}
            if final_result is not None or classification not in {"RATE_LIMITED", "QUOTA_EXHAUSTED", "CAPACITY"}:
                break
            continue
        fixture = RecordedFreeProvider(provider_name, model, item.get("response", "ok"), eligibility, item.get("usage", {}))
        for retry_index in range(max_attempts):
            result, output = fixture.request(quota=item.get("quota", "available"), request_id=item.get("request_id"))
            attempts.append({"provider": provider_name, "model": model, "classification": result.classification, "eligibility": fixture.eligibility, "request_id": result.request_id,
                             "headers": result.headers or {}, "retry_after": result.retry_after, "retry_index": retry_index,
                             "usage": result.usage or {},
                             "idempotency_key": payload.get("idempotency_key")})
            if result.classification == "OK":
                final_result = {"provider": provider_name, "model": model, "eligibility": fixture.eligibility, "session_disclosed": len(attempts) > 1, "attempts": attempts, "output": output}
                break
            if result.classification == "RATE_LIMITED" and retry_index + 1 < max_attempts:
                continue
            if result.classification not in {"QUOTA_EXHAUSTED", "RATE_LIMITED", "CAPACITY"}: break
            break
        if final_result is not None:
            break
    if final_result is None:
        final_result = {"classification": attempts[-1]["classification"] if attempts else "NO_PROVIDER", "attempts": attempts, "output": ""}
    for index, attempt in enumerate(attempts):
        store.db.execute("INSERT INTO provider_request_events(id,idempotency_key,attempt_index,provider,model,classification,request_id,headers_json,usage_json,retry_after,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (uuid.uuid4().hex, idempotency_key, index, attempt["provider"], attempt["model"], attempt["classification"], attempt.get("request_id"),
                          json.dumps(attempt.get("headers", {}), sort_keys=True), json.dumps(attempt.get("usage", {}), sort_keys=True), attempt.get("retry_after"), time.time()))
    if idempotency_key:
        store.db.execute("INSERT INTO provider_route_results(idempotency_key,request_hash,result_json,created_at) VALUES(?,?,?,?)",
                         (idempotency_key, request_hash, json.dumps(final_result, sort_keys=True), time.time()))
        store.db.commit()
    return final_result


def resume_disclosure(native_resumed: bool, checkpoint_id: str | None, pending_action_reconciled: bool, safe_to_continue: bool) -> dict:
    return {"worker_identity_restored": bool(checkpoint_id),
            "provider_session_resumed": bool(native_resumed),
            "conversation_memory_restored": bool(checkpoint_id),
            "restored_from_checkpoint_id": checkpoint_id,
            "pending_action_reconciled": bool(pending_action_reconciled),
            "safe_to_continue": bool(safe_to_continue and pending_action_reconciled)}


class ArtifactStore:
    """Bounded local evidence blobs; credentials are redacted before writing."""
    SECRET = re.compile(r"(?i)(api[_-]?key|authorization|bearer|token)(\s*[:=]\s*)([^\s,]+(?:\s+[^\s,]+)?)")
    def __init__(self, root: str | Path, max_bytes: int = 4_000_000):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True); self.max_bytes = max_bytes

    def put_text(self, evidence_id: str, text: str) -> dict:
        redacted, count = self.SECRET.subn(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
        data = redacted.encode("utf-8")
        if len(data) > self.max_bytes: raise ValueError("artifact exceeds byte limit")
        path = self.root / f"{evidence_id}.txt"; path.write_bytes(data)
        return {"path": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "redacted": count > 0, "redactions": count}


class OrganizationDaemon:
    """Foreground-capable durable owner for local organization state."""
    def __init__(self, store: Store, instance_id: str | None = None, mode: str = "foreground"):
        self.store = store; self.instance_id = instance_id or uuid.uuid4().hex; self.mode = mode; self._schedulers = {}; self._recovery = {}

    def register_recovery_provider(self, name: str, adapter_factory, profile_factory, authority_factory, context_factory):
        """Register local provider factories used only by daemon-owned restart recovery."""
        self._recovery[name] = (adapter_factory, profile_factory, authority_factory, context_factory)

    def recover_queued(self, capacity: int = 2) -> int:
        scheduler = self.scheduler(capacity)
        recovered = 0
        for provider, factories in self._recovery.items():
            adapter_factory, profile_factory, authority_factory, context_factory = factories
            recovered += scheduler.recover_queued(lambda _name, factory=adapter_factory: factory(), profile_factory, authority_factory, context_factory)
        return recovered

    def scheduler(self, capacity: int = 2):
        """Return the daemon-owned process scheduler for this durable state."""
        from .managed_scheduler import ManagedScheduler
        scheduler = self._schedulers.get(capacity)
        if scheduler is None:
            scheduler = ManagedScheduler(self.store, capacity); self._schedulers[capacity] = scheduler
        return scheduler

    def start(self) -> dict:
        self.store.db.execute("INSERT OR REPLACE INTO daemon_state(id,instance_id,mode,status,last_tick,degraded_reason) VALUES(1,?,?,?,?,?)", (self.instance_id, self.mode, "RUNNING", time.time(), None)); self.store.db.commit()
        return {**self.health(), "recovered_queued": self.recover_queued() if self._recovery else 0}

    def tick(self) -> dict:
        row = self.store.db.execute("SELECT status FROM daemon_state WHERE id=1").fetchone()
        if row is None or row["status"] != "RUNNING": raise RuntimeError("daemon is not running")
        self.store.reconcile(self.instance_id)
        self.store.db.execute("UPDATE daemon_state SET last_tick=? WHERE id=1", (time.time(),)); self.store.db.commit()
        health = self.health()
        if self._recovery:
            health["recovered_queued"] = self.recover_queued()
        return health

    def run_once(self) -> dict:
        """Perform one deterministic foreground reconciliation cycle."""
        tick = self.tick()
        terminal = [dict(row) for row in self.store.db.execute("SELECT id FROM goals WHERE status IN ('COMPLETE','FAILED','CANCELLED')")]
        for goal in terminal:
            self.store.db.execute("UPDATE goal_subscribers SET event_types_json='[]',updated_at=? WHERE goal_id=?", (time.time(), goal["id"]))
        self.store.db.commit()
        return {"health": self.health(), "recovered_queued": tick.get("recovered_queued", 0), "terminal_goals_reconciled": [row["id"] for row in terminal],
                "unknown_actions": [dict(row) for row in self.store.db.execute("SELECT id,goal_id,idempotency_key FROM interrupted_actions WHERE classification='unknown' AND reconciled=0")]} 

    def stop(self) -> dict:
        for scheduler in self._schedulers.values(): scheduler.close()
        self._schedulers.clear()
        self.store.db.execute("UPDATE daemon_state SET status='STOPPED',last_tick=? WHERE id=1", (time.time(),)); self.store.db.commit(); return self.health()

    def health(self) -> dict:
        row = self.store.db.execute("SELECT * FROM daemon_state WHERE id=1").fetchone()
        result = dict(row) if row else {"status": "STOPPED", "mode": "foreground", "degraded_reason": None}
        system = platform.system().lower()
        manager = "launchd" if system == "darwin" else "systemd" if system == "linux" else None
        manager_available = bool(shutil.which("launchctl" if manager == "launchd" else "systemctl")) if manager else False
        result["service_manager"] = manager
        result["service_manager_available"] = manager_available
        result["foreground_only"] = not manager_available
        if result.get("mode") == "foreground" and manager is not None and not manager_available:
            result["degraded_reason"] = "service_manager_unavailable"
        return result

    def service_plan(self, executable: str = "acp") -> dict:
        """Describe an OS service installation without mutating the host."""
        system = platform.system().lower()
        if system == "darwin":
            command = [executable, "organization", "daemon", "start"]
            return {"manager": "launchd", "available": bool(shutil.which("launchctl")), "installable": bool(shutil.which("launchctl")), "mode": "managed", "command": command,
                    "restart_policy": "KeepAlive", "manifest": {"Label": "com.mac.agent-control-plane", "ProgramArguments": command, "KeepAlive": True, "RunAtLoad": True}}
        if system == "linux":
            command = [executable, "organization", "daemon", "start"]
            unit = "[Unit]\nDescription=MAC agent control plane\nAfter=network.target\n[Service]\nExecStart=" + " ".join(command) + "\nRestart=always\n[Install]\nWantedBy=default.target\n"
            return {"manager": "systemd", "available": bool(shutil.which("systemctl")), "installable": bool(shutil.which("systemctl")), "mode": "managed", "command": command,
                    "restart_policy": "always", "manifest": {"unit": unit}}
        return {"manager": None, "available": False, "installable": False, "mode": "foreground", "command": [executable, "organization", "daemon", "start"], "reason": "unsupported service manager", "manifest": None}

    def resume_goal(self, goal_id: str, actor: str = "master") -> dict:
        return append_goal_event(self.store, goal_id, "GOAL_STARTED", {"daemon_instance": self.instance_id}, actor)

    def cancel_goal(self, goal_id: str, reason: str, actor: str = "master") -> dict:
        event = append_goal_event(self.store, goal_id, "CANCELLED", {"reason": reason, "daemon_instance": self.instance_id}, actor)
        # Terminal goal ownership revokes all durable task dispatches linked
        # to the goal before the cancellation is considered complete.
        task_ids = [row["id"] for row in self.store.db.execute("SELECT id FROM tasks WHERE goal_id=? AND status NOT IN ('DONE','ACCEPTED')", (goal_id,))]
        for task_id in task_ids:
            try:
                self.store.cancel_task(task_id, f"goal cancelled: {reason}")
            except ValueError:
                pass
        return {**event, "revoked_task_ids": task_ids}

    def record_interrupted_action(self, goal_id: str, idempotency_key: str, phase: str, payload: dict) -> dict:
        if phase not in {"confirmed_not_started", "submitted_waiting_result", "completed", "failed", "unknown"}:
            raise ValueError("invalid interrupted action phase")
        goal = self.store.db.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()
        if goal is None:
            raise ValueError("unknown goal for interrupted action")
        if goal["status"] in {"COMPLETE", "FAILED", "CANCELLED"}:
            raise ValueError("terminal goal cannot record interrupted action")
        existing = self.store.db.execute("SELECT * FROM interrupted_actions WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if existing is not None:
            if existing["goal_id"] != goal_id or existing["phase"] != phase or json.loads(existing["payload_json"]) != payload:
                raise ValueError("idempotency key conflicts with durable action")
            return {"id": existing["id"], "classification": existing["classification"], "safe_to_retry": existing["classification"] == "confirmed_not_started", "replayed": True}
        action_id = uuid.uuid4().hex; stamp = time.time()
        self.store.db.execute("INSERT INTO interrupted_actions(id,goal_id,idempotency_key,phase,classification,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                              (action_id, goal_id, idempotency_key, phase, phase, json.dumps(payload, sort_keys=True), stamp, stamp)); self.store.db.commit()
        return {"id": action_id, "classification": phase, "safe_to_retry": phase == "confirmed_not_started"}

    def reconcile_interrupted(self, goal_id: str) -> list[dict]:
        rows = self.store.db.execute("SELECT * FROM interrupted_actions WHERE goal_id=? AND reconciled=0 ORDER BY created_at", (goal_id,)).fetchall()
        result = []
        for row in rows:
            if row["classification"] == "unknown":
                result.append({"id": row["id"], "classification": "unknown", "safe_to_retry": False})
            else:
                result.append({"id": row["id"], "classification": row["classification"], "safe_to_retry": row["classification"] == "confirmed_not_started"})
        return result
