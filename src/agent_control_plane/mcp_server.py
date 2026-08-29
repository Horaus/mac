"""Minimal stdio JSON-RPC/MCP-compatible facade for local supervisor tooling."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .store import Store
from .providers import provider
from .service import (arbitrate_conflict, cancel_managed_worker, finish_managed_worker,
                       pause_managed_worker, resume_live_worker, run_worker,
                       start_managed_worker, validate)
from .orchestrator import Supervisor


TOOL_DEFS = {
    "status": {}, "inbox": {}, "reconcile": {},
    "control_status": {},
    "control_set_policy": {"policy": {"type": "string"}},
    "control_register_master": {"master_id": {"type": "string"}, "name": {"type": "string"}, "min_workers": {"type": "number"}, "max_workers": {"type": "number"}},
    "control_request_master_workers": {"master_id": {"type": "string"}, "requested": {"type": "number"}},
    "control_release_master": {"master_id": {"type": "string"}},
    "control_register_boss": {"boss_id": {"type": "string"}, "name": {"type": "string"}, "min_workers": {"type": "number"}, "max_workers": {"type": "number"}},
    "control_request_workers": {"boss_id": {"type": "string"}, "requested": {"type": "number"}},
    "control_release_boss": {"boss_id": {"type": "string"}},
    "history_add": {"conversation_id": {"type": "string"}, "role": {"type": "string"}, "actor": {"type": "string"}, "content": {"type": "string"}, "task_id": {"type": "string"}, "worker_id": {"type": "string"}},
    "history_list": {"conversation_id": {"type": "string"}, "limit": {"type": "number"}},
    "knowledge_load": {"path": {"type": "string"}},
    "knowledge_ack": {"actor": {"type": "string"}, "worker_id": {"type": "string"}},
    "declare_resource": {"name": {"type": "string"}, "kind": {"type": "string"}, "paths": {"type": "array", "items": {"type": "string"}}},
    "acquire_resource": {"resource": {"type": "string"}, "task_id": {"type": "string"}, "worker_id": {"type": "string"}, "mode": {"type": "string"}, "ttl": {"type": "number"}},
    "renew_resource": {"resource": {"type": "string"}, "task_id": {"type": "string"}, "worker_id": {"type": "string"}, "ttl": {"type": "number"}},
    "bump_resource": {"resource": {"type": "string"}},
    "accept_integration": {"task_id": {"type": "string"}, "worktree": {"type": "string"}, "commit_message": {"type": "string"}, "validation": {"type": "array", "items": {"type": "string"}}},
    "arbitrate_conflict": {"task_id": {"type": "string"}, "decision_id": {"type": "string"}, "provider": {"type": "string"}, "evidence": {"type": "string"}, "cwd": {"type": "string"}},
    "create_task": {"id": {"type": "string"}, "title": {"type": "string"}, "provider": {"type": "string"}, "depends_on": {"type": "array", "items": {"type": "string"}}},
    "create_resource": {"name": {"type": "string"}, "kind": {"type": "string"}, "paths": {"type": "array", "items": {"type": "string"}}},
    "send_message": {"type": {"type": "string"}, "payload": {"type": "object"}, "task_id": {"type": "string"}, "worker_id": {"type": "string"}},
    "accept_task": {"task_id": {"type": "string"}}, "pause_task": {"task_id": {"type": "string"}, "reason": {"type": "string"}},
    "resume_task": {"task_id": {"type": "string"}}, "cancel_task": {"task_id": {"type": "string"}, "reason": {"type": "string"}},
    "resolve_conflict": {"decision_id": {"type": "string"}, "task_id": {"type": "string"}, "decision": {"type": "string"}, "reason": {"type": "string"}},
    "run_worker": {"task_id": {"type": "string"}, "worker_id": {"type": "string"}, "provider": {"type": "string"}, "prompt": {"type": "string"}, "cwd": {"type": "string"}, "conversation_id": {"type": "string"}},
    "start_worker": {"task_id": {"type": "string"}, "worker_id": {"type": "string"}, "provider": {"type": "string"}, "prompt": {"type": "string"}, "cwd": {"type": "string"}, "conversation_id": {"type": "string"}},
    "pause_worker": {"task_id": {"type": "string"}, "worker_id": {"type": "string"}},
    "resume_worker": {"task_id": {"type": "string"}, "worker_id": {"type": "string"}},
    "wait_worker": {"task_id": {"type": "string"}, "worker_id": {"type": "string"}},
    "cancel_worker": {"task_id": {"type": "string"}, "worker_id": {"type": "string"}, "reason": {"type": "string"}},
    "validate": {"task_id": {"type": "string"}, "command": {"type": "string"}, "cwd": {"type": "string"}, "timeout": {"type": "number"}},
}

# Managed runs are deliberately process-local: the MCP server owns the
# subprocess handle, while durable task/run state remains in SQLite.
ACTIVE_RUNS = {}

def _history_enabled(store: Store) -> bool:
    config = store.path.parent / "config.json"
    if not config.exists(): return True
    try: return json.loads(config.read_text()).get("control", {}).get("policy", "lock") == "lock"
    except (OSError, json.JSONDecodeError): return True


def dispatch(store: Store, method: str, args: dict) -> object:
    if method == "ping":
        return {}
    if method == "initialize":
        return {"protocolVersion": "2024-11-05", "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "agent-control-plane", "version": "0.1.0"},
                "instructions": "You are the Master supervisor. Use MAC Control to register your master_id and request a complete worker group before dispatch. In lock mode, worker capacity is fixed and shortages return PENDING/rejected; flexible mode permits temporary worker counts but does not persist chat history. In lock mode preserve master_id and conversation_id for history; do not silently continue a changed conversation. Use isolated worktrees, never merge worker changes, validate before acceptance, and report status, validation, commit, blockers, and conflicts."}
    if method == "tools/list":
        return {"tools": [{"name": name, "description": f"control-plane {name}",
                            "inputSchema": {"type": "object", "properties": props}}
                         for name, props in TOOL_DEFS.items()]}
    if method != "tools/call":
        raise ValueError(f"unsupported method: {method}")
    name, a = args.get("name"), args.get("arguments", {})
    if name == "status": return {"content": [{"type": "text", "text": json.dumps(store.snapshot())}]}
    if name == "control_status": return {"content": [{"type": "text", "text": json.dumps(store.control_snapshot(), ensure_ascii=False)}]}
    if name == "control_set_policy":
        store.control_set_policy(a["policy"]); return {"content": [{"type": "text", "text": "control policy updated"}]}
    if name == "control_register_master":
        store.control_register_boss(a["master_id"], a.get("name"), int(a.get("min_workers", 1)), int(a.get("max_workers", a.get("min_workers", 1))))
        return {"content": [{"type": "text", "text": "master registered"}]}
    if name == "control_request_master_workers":
        return {"content": [{"type": "text", "text": json.dumps(store.control_request_workers(a["master_id"], a.get("requested")))}]}
    if name == "control_release_master":
        return {"content": [{"type": "text", "text": json.dumps(store.control_release_boss(a["master_id"]))}]}
    if name == "control_register_boss":
        store.control_register_boss(a["boss_id"], a.get("name"), int(a.get("min_workers", 1)), int(a.get("max_workers", a.get("min_workers", 1))))
        return {"content": [{"type": "text", "text": "boss registered"}]}
    if name == "control_request_workers":
        return {"content": [{"type": "text", "text": json.dumps(store.control_request_workers(a["boss_id"], a.get("requested")))}]}
    if name == "control_release_boss":
        return {"content": [{"type": "text", "text": json.dumps(store.control_release_boss(a["boss_id"]))}]}
    if name == "history_add":
        store.record_history(a["conversation_id"], a["role"], a["actor"], a["content"], a.get("task_id"), a.get("worker_id"))
        return {"content": [{"type": "text", "text": "recorded"}]}
    if name == "history_list":
        return {"content": [{"type": "text", "text": json.dumps([dict(row) for row in store.history(a["conversation_id"], a.get("limit", 50))], ensure_ascii=False)}]}
    if name == "knowledge_load":
        return {"content": [{"type": "text", "text": json.dumps({"digest": store.load_knowledge(a["path"])})}]}
    if name == "knowledge_ack":
        store.acknowledge_knowledge(a["actor"], a.get("worker_id")); return {"content": [{"type": "text", "text": "acknowledged"}]}
    if name == "run_worker":
        prompt = a["prompt"]
        if a.get("conversation_id") and _history_enabled(store):
            context = list(reversed(store.history(a["conversation_id"], 20)))
            prompt += "\n\nSHARED BOSS/WORKER HISTORY:\n" + "\n".join(f"[{row['role']}/{row['actor']}] {row['content']}" for row in context)
        result = run_worker(store, a["task_id"], a["worker_id"], provider(a.get("provider", "codex")), prompt, a["cwd"])
        return {"content": [{"type": "text", "text": json.dumps({"exit_code": result.exit_code, "session_id": result.session_id, "status": store.task(a["task_id"])["status"]})}]}
    if name == "start_worker":
        key = (a["task_id"], a["worker_id"])
        if key in ACTIVE_RUNS:
            raise ValueError("managed worker is already active")
        prompt = a["prompt"]
        if a.get("conversation_id") and _history_enabled(store):
            context = list(reversed(store.history(a["conversation_id"], 20)))
            prompt += "\n\nSHARED BOSS/WORKER HISTORY:\n" + "\n".join(f"[{row['role']}/{row['actor']}] {row['content']}" for row in context)
        run = start_managed_worker(store, a["task_id"], a["worker_id"], provider(a.get("provider", "codex")), prompt, a["cwd"])
        ACTIVE_RUNS[key] = run
        return {"content": [{"type": "text", "text": json.dumps({"session_id": run.session_id, "status": "RUNNING"})}]}
    if name == "pause_worker":
        key = (a["task_id"], a["worker_id"])
        run = ACTIVE_RUNS.get(key)
        if run is None: raise ValueError("managed worker is not active")
        pause_managed_worker(store, *key, run)
        return {"content": [{"type": "text", "text": "paused"}]}
    if name == "resume_worker":
        key = (a["task_id"], a["worker_id"])
        run = ACTIVE_RUNS.get(key)
        if run is None: raise ValueError("managed worker is not active")
        resume_live_worker(store, *key, run)
        return {"content": [{"type": "text", "text": "resumed"}]}
    if name == "wait_worker":
        key = (a["task_id"], a["worker_id"])
        run = ACTIVE_RUNS.pop(key, None)
        if run is None: raise ValueError("managed worker is not active")
        result = finish_managed_worker(store, *key, run)
        return {"content": [{"type": "text", "text": json.dumps({"exit_code": result.exit_code, "session_id": result.session_id, "status": store.task(a["task_id"])["status"]})}]}
    if name == "cancel_worker":
        key = (a["task_id"], a["worker_id"])
        run = ACTIVE_RUNS.pop(key, None)
        if run is None: raise ValueError("managed worker is not active")
        cancel_managed_worker(store, *key, run, a.get("reason", "supervisor cancellation"))
        return {"content": [{"type": "text", "text": "cancelled"}]}
    if name == "validate":
        exit_code = validate(store, a["task_id"], a["command"], a["cwd"], a.get("timeout", 300))
        return {"content": [{"type": "text", "text": json.dumps({"exit_code": exit_code})}]}
    if name == "reconcile": return {"content": [{"type": "text", "text": json.dumps({"recovered_tasks": store.reconcile()})}]}
    if name == "declare_resource":
        store.add_resource(a["name"], a["kind"], a.get("paths", [])); return {"content": [{"type": "text", "text": f"created {a['name']}"}]}
    if name == "acquire_resource":
        acquired = store.acquire(a["resource"], a["task_id"], a["worker_id"], a["mode"], a.get("ttl", 300))
        return {"content": [{"type": "text", "text": json.dumps({"acquired": acquired})}]}
    if name == "renew_resource":
        renewed = store.renew(a["resource"], a["task_id"], a["worker_id"], a.get("ttl", 300))
        return {"content": [{"type": "text", "text": json.dumps({"renewed": renewed})}]}
    if name == "bump_resource":
        version = store.bump_resource(a["resource"]); return {"content": [{"type": "text", "text": json.dumps({"version": version})}]}
    if name == "create_task":
        store.add_task(a["id"], a["title"], a.get("provider"), a.get("depends_on", [])); return {"content": [{"type": "text", "text": f"created {a['id']}"}]}
    if name == "create_resource":
        store.add_resource(a["name"], a["kind"], a.get("paths", [])); return {"content": [{"type": "text", "text": f"created {a['name']}"}]}
    if name == "send_message":
        store.add_message(a["type"], a.get("payload", {}), a.get("task_id"), a.get("worker_id")); return {"content": [{"type": "text", "text": "queued"}]}
    if name == "accept_task": store.accept_task(a["task_id"]); return {"content": [{"type": "text", "text": f"accepted {a['task_id']}"}]}
    if name == "accept_integration":
        project = store.path.parent.parent
        supervisor = Supervisor(project, store=store)
        try:
            commit = supervisor.accept(a["task_id"], a["worktree"], a["commit_message"], tuple(a.get("validation", [])))
        finally:
            # The MCP server owns the store connection and closes it at shutdown.
            pass
        return {"content": [{"type": "text", "text": json.dumps({"commit": commit, "status": store.task(a["task_id"])["status"]})}]}
    if name == "arbitrate_conflict":
        result = arbitrate_conflict(store, a["task_id"], a["decision_id"], provider(a.get("provider", "codex")), a["evidence"], a["cwd"])
        return {"content": [{"type": "text", "text": json.dumps({"exit_code": result.exit_code, "status": store.task(a["task_id"])["status"]})}]}
    if name == "pause_task": store.pause_task(a["task_id"], a.get("reason", "supervisor pause")); return {"content": [{"type": "text", "text": f"paused {a['task_id']}"}]}
    if name == "resume_task": store.resume_task(a["task_id"]); return {"content": [{"type": "text", "text": f"resumed {a['task_id']}"}]}
    if name == "cancel_task":
        task = store.task(a["task_id"])
        key = (a["task_id"], task["worker_id"]) if task and task["worker_id"] else None
        run = ACTIVE_RUNS.pop(key, None) if key else None
        if run is not None:
            cancel_managed_worker(store, *key, run, a.get("reason", "supervisor cancellation"))
        else:
            store.cancel_task(a["task_id"], a.get("reason", "supervisor cancellation"))
        return {"content": [{"type": "text", "text": f"cancelled {a['task_id']}"}]}
    if name == "resolve_conflict":
        store.resolve_conflict(a["decision_id"], a["task_id"], a["decision"], a["reason"]); return {"content": [{"type": "text", "text": f"resolved {a['task_id']}"}]}
    if name == "inbox": return {"content": [{"type": "text", "text": json.dumps([dict(row) for row in store.inbox()])}]}
    raise ValueError(f"unknown tool: {name}")


def serve(state_path: str | Path) -> None:
    store = Store(state_path)
    try:
        for line in sys.stdin:
            if not line.strip(): continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as error:
                print(json.dumps({"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32700, "message": f"parse error: {error.msg}"}}), flush=True)
                continue
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
                print(json.dumps({"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                                  "error": {"code": -32600, "message": "invalid request"}}), flush=True)
                continue
            # JSON-RPC notifications, including MCP initialized, have no id
            # and must not receive a response.
            if "id" not in request:
                # JSON-RPC notifications never receive a response, including
                # custom no-id methods that the server may choose to ignore.
                try:
                    dispatch(store, request["method"], request.get("params", {}))
                except Exception:
                    pass
                continue
            response = {"jsonrpc": "2.0", "id": request.get("id")}
            try: response["result"] = dispatch(store, request["method"], request.get("params", {}))
            except Exception as error: response["error"] = {"code": -32000, "message": str(error)}
            print(json.dumps(response), flush=True)
    finally:
        # A client disconnect must not orphan provider processes owned by this
        # MCP server. Persist cancellation before closing the durable store.
        for key, run in list(ACTIVE_RUNS.items()):
            try:
                cancel_managed_worker(store, key[0], key[1], run, "MCP server shutdown")
            except Exception:
                run.cancel()
                run.wait()
            finally:
                ACTIVE_RUNS.pop(key, None)
        store.close()
