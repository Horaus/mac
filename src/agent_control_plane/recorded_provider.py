"""Offline provider fixtures for deterministic usage/benchmark acceptance tests."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import urllib.request
import urllib.error

from .providers import ProviderAdapter, WorkerResult
from .providers import parse_usage
from .organization_runtime import RecordedFreeProvider


@dataclass(frozen=True)
class RecordedTurn:
    output: str = "recorded output"
    input_tokens: int = 100
    cached_input_tokens: int = 20
    output_tokens: int = 12
    files_read: int = 3
    commands_executed: int = 2
    tool_output_bytes: int = 64


class RecordedProviderAdapter(ProviderAdapter):
    name = "recorded-provider"

    def __init__(self, turn: RecordedTurn = RecordedTurn()):
        self.turn = turn

    def run(self, prompt: str, cwd: Path, profile=None):
        limit = getattr(getattr(profile, "context", None), "max_input_tokens", None)
        if limit is not None and self.turn.input_tokens > limit:
            return WorkerResult(2, "hard input budget denied", "recorded-denied",
                                {"input_tokens": self.turn.input_tokens,
                                 "cached_input_tokens": self.turn.cached_input_tokens,
                                 "output_tokens": 0, "files_read": 0,
                                 "commands_executed": 0, "tool_output_bytes": 0},
                                "hard_budget_denied")
        return WorkerResult(0, self.turn.output, "recorded-session",
                            {"input_tokens": self.turn.input_tokens,
                             "cached_input_tokens": self.turn.cached_input_tokens,
                             "output_tokens": self.turn.output_tokens,
                             "files_read": self.turn.files_read,
                            "commands_executed": self.turn.commands_executed,
                             "tool_output_bytes": self.turn.tool_output_bytes}, None)


class RecordedFreeAPIAdapter(ProviderAdapter):
    """ProviderAdapter wrapper for offline Gemini/Cloudflare HTTP fixtures."""
    def __init__(self, provider: str, model: str = "free-fixture", quota: str = "available"):
        self.name = provider; self.fixture = RecordedFreeProvider(provider, model); self.quota = quota

    def run(self, prompt: str, cwd: Path, profile=None, context_packet=None):
        result, output = self.fixture.request(quota=self.quota)
        if result.classification != "OK":
            return WorkerResult(2, result.classification, result.request_id, {"quota_classification": result.classification, "retry_after": result.retry_after, "reset_at": result.reset_at}, result.classification)
        return WorkerResult(0, output or prompt[:80], result.request_id, {"request_id": result.request_id, "provider": result.provider, "model": result.model, "quota_classification": "OK"})


class MockHTTPTransport:
    """Deterministic HTTP transport used by provider acceptance tests."""
    def __init__(self, responses):
        self.responses = list(responses); self.requests = []

    def request(self, method: str, url: str, headers: dict, body: dict):
        self.requests.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        if not self.responses:
            raise RuntimeError("mock transport exhausted")
        return self.responses.pop(0)


class UrllibHTTPTransport:
    """Small real HTTP transport; tests inject MockHTTPTransport instead."""
    def request(self, method: str, url: str, headers: dict, body: dict):
        request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                         headers={**headers, "Content-Type": "application/json"}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
                return {"status": response.status, "headers": dict(response.headers), "json": json.loads(raw or "{}")}
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8")
            return {"status": error.code, "headers": dict(error.headers), "json": json.loads(raw or "{}")}


def resolve_secret_ref(secret_ref: str, resolver=None) -> str:
    """Resolve a reference without allowing adapters to confuse it with a key."""
    if not isinstance(secret_ref, str) or not secret_ref:
        raise ValueError("secret reference is required")
    if "://" in secret_ref:
        scheme, name = secret_ref.split("://", 1)
        if scheme == "keychain":
            from .secret_store import load_secret
            return load_secret(secret_ref)
        if scheme != "env" or not name or "://" in name:
            raise ValueError("unsupported secret reference scheme")
        lookup = name
    else:
        lookup = secret_ref
    if resolver is not None:
        value = resolver(secret_ref)
    else:
        value = os.environ.get(lookup) or os.environ.get("MAC_SECRET_" + lookup)
    if not value:
        raise ValueError("secret reference could not be resolved")
    return value


class HTTPFreeProviderAdapter(ProviderAdapter):
    provider = ""
    endpoint = ""

    def __init__(self, api_key_ref: str, model: str, transport=None, endpoint: str | None = None, secret_resolver=None):
        if not api_key_ref: raise ValueError("secret reference is required")
        self.api_key_ref, self.model = api_key_ref, model
        self.secret_resolver = secret_resolver
        self.transport = transport or UrllibHTTPTransport()
        if endpoint: self.endpoint = endpoint

    def run(self, prompt: str, cwd: Path, profile=None, context_packet=None):
        secret = resolve_secret_ref(self.api_key_ref, self.secret_resolver)
        body = {"model": self.model, "prompt": prompt}
        response = self.transport.request("POST", self.endpoint, {"Authorization": "Bearer " + secret}, body)
        status = int(response.get("status", 500)); headers = dict(response.get("headers", {})); data = response.get("json", {})
        usage = dict(data.get("usage", {})) if isinstance(data, dict) else {}
        usage["response_class"] = "OK" if status < 400 else "HTTP_ERROR"
        usage["rate_limit_headers"] = headers
        if status == 401: return WorkerResult(2, "AUTH_ERROR", data.get("request_id"), usage, "AUTH_ERROR")
        if status == 429: return WorkerResult(2, "RATE_LIMITED", data.get("request_id"), usage, "RATE_LIMITED")
        if status >= 400: return WorkerResult(2, "PROVIDER_ERROR", data.get("request_id"), usage, "PROVIDER_ERROR")
        output = data.get("output", data.get("response", ""))
        return WorkerResult(0, output, data.get("request_id"), usage, None)

    def _result(self, response, output_fn):
        status = int(response.get("status", 500)); headers = dict(response.get("headers", {})); data = response.get("json", {})
        usage = dict(data.get("usage", {})) if isinstance(data, dict) else {}
        usage.update({"response_class": "OK" if status < 400 else "HTTP_ERROR", "rate_limit_headers": headers})
        request_id = data.get("request_id") or headers.get("cf-ray")
        if status == 401: return WorkerResult(2, "AUTH_ERROR", request_id, usage, "AUTH_ERROR")
        if status == 429:
            classification = classify_http_429(data, headers)
            return WorkerResult(2, classification, request_id, usage, classification)
        if status >= 400: return WorkerResult(2, "PROVIDER_ERROR", request_id, usage, "PROVIDER_ERROR")
        return WorkerResult(0, output_fn(data), request_id, usage, None)


def classify_http_429(data: dict, headers: dict) -> str:
    text = json.dumps(data, sort_keys=True).lower()
    if any(token in text for token in ("capacity", "overloaded", "resource_exhausted_capacity")):
        return "CAPACITY"
    if any(token in text for token in ("quota", "daily limit", "resource_exhausted")):
        return "QUOTA_EXHAUSTED"
    return "RATE_LIMITED" if headers.get("retry-after") or headers.get("Retry-After") else "RATE_LIMITED"


class GeminiAPIAdapter(HTTPFreeProviderAdapter):
    provider = "google-gemini"
    endpoint = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, api_key_ref, model, transport=None, secret_resolver=None, endpoint=None):
        super().__init__(api_key_ref, model, transport, endpoint, secret_resolver)
        self.endpoint = endpoint or f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def run(self, prompt, cwd, profile=None, context_packet=None):
        secret = resolve_secret_ref(self.api_key_ref, self.secret_resolver)
        response = self.transport.request("POST", self.endpoint, {"x-goog-api-key": secret},
                                          {"contents": [{"role": "user", "parts": [{"text": prompt}]}]})
        result = self._result(response, lambda data: ((data.get("candidates") or [{}])[0].get("content", {}).get("parts") or [{"text": ""}])[0].get("text", ""))
        data = response.get("json", {})
        metadata = data.get("usageMetadata", {}) if isinstance(data, dict) else {}
        result.usage.update({"input_tokens": metadata.get("promptTokenCount", result.usage.get("input_tokens")),
                             "cached_input_tokens": metadata.get("cachedContentTokenCount", result.usage.get("cached_input_tokens")),
                             "output_tokens": metadata.get("candidatesTokenCount", result.usage.get("output_tokens")),
                             "total_tokens": metadata.get("totalTokenCount", result.usage.get("total_tokens"))})
        return result


class CloudflareWorkersAIAdapter(HTTPFreeProviderAdapter):
    provider = "cloudflare-workers-ai"
    endpoint = "https://api.cloudflare.com/client/v4/accounts"

    def __init__(self, api_key_ref, model, account_id, transport=None, secret_resolver=None, endpoint=None):
        super().__init__(api_key_ref, model, transport, endpoint, secret_resolver)
        self.account_id = account_id
        self.endpoint = endpoint or f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"

    def run(self, prompt, cwd, profile=None, context_packet=None):
        secret = resolve_secret_ref(self.api_key_ref, self.secret_resolver)
        response = self.transport.request("POST", self.endpoint, {"Authorization": "Bearer " + secret},
                                          {"messages": [{"role": "user", "content": prompt}]})
        result = self._result(response, lambda data: data.get("result", {}).get("response", data.get("response", "")))
        data = response.get("json", {}); provider_usage = data.get("result", {}).get("usage", {}) if isinstance(data, dict) else {}
        result.usage.update(provider_usage)
        return result

    def _result(self, response, output):
        status = int(response.get("status", 500)); headers = dict(response.get("headers", {})); data = response.get("json", {})
        usage = dict(data.get("usage", {})) if isinstance(data, dict) else {}
        usage.update({"response_class": "OK" if status < 400 else "HTTP_ERROR", "rate_limit_headers": headers})
        request_id = data.get("request_id") or headers.get("cf-ray")
        if status == 401: return WorkerResult(2, "AUTH_ERROR", request_id, usage, "AUTH_ERROR")
        if status == 429:
            classification = classify_http_429(data, headers)
            return WorkerResult(2, classification, request_id, usage, classification)
        if status >= 400: return WorkerResult(2, "PROVIDER_ERROR", request_id, usage, "PROVIDER_ERROR")
        return WorkerResult(0, output(data), request_id, usage, None)


class SessionFixtureAdapter(ProviderAdapter):
    """Offline provider-native continuity fixture; conversation history is ignored."""
    name = "session-fixture"

    def __init__(self):
        self.sessions: dict[str, str] = {}
        self.counter = 0

    def run(self, prompt: str, cwd: Path, profile=None, context_packet=None):
        self.counter += 1
        session = f"fixture-session-{self.counter}"
        marker = prompt.split("MARKER:", 1)[1].strip() if "MARKER:" in prompt else ""
        self.sessions[session] = marker
        return WorkerResult(0, marker, session, {"input_tokens": 1, "output_tokens": 1})

    def resume(self, session_id: str, prompt: str, cwd: Path, profile=None, resume_snapshot=False):
        adapter = self
        class Completed:
            def __init__(self):
                self.session_id = session_id
                self.cwd = Path(cwd)
                self.adapter = adapter
            def wait(self, timeout=None):
                if session_id not in adapter.sessions:
                    return WorkerResult(1, "provider session missing", session_id, {}, "SESSION_NOT_FOUND")
                return WorkerResult(0, adapter.sessions[session_id], session_id, {"input_tokens": 1, "output_tokens": 1})
        return Completed()


def benchmark_recorded_luna_sol() -> dict:
    """Comparable offline benchmark fixture; real account entitlement is not assumed."""
    return {name: RecordedProviderAdapter().turn.__dict__.copy() for name in ("luna", "sol")}


def budget_fixture_matrix() -> dict:
    """Recorded observations used to distinguish per-event from cumulative usage."""
    return {
        "event_4k": '{"usage":{"input_tokens":4000}}',
        "event_12k": '{"usage":{"input_tokens":12000}}',
        "event_16k": '{"usage":{"input_tokens":16000}}',
        "cumulative_12k_to_650k": "\n".join(
            '{"cumulative":true,"usage":{"input_tokens":%d}}' % value
            for value in (12000, 180000, 650000)
        ),
    }


def parsed_budget_fixture_matrix() -> dict:
    return {name: parse_usage(value)["input_tokens"] for name, value in budget_fixture_matrix().items()}
