"""Durable v0.5 organization domain services.

The module deliberately keeps policy in Python and uses the existing Store
connection. Provider adapters remain responsible only for provider behavior.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from dataclasses import dataclass
from typing import Any

from .store import Store

TERMINAL_GOAL_STATES = {"COMPLETE", "FAILED", "CANCELLED"}
GOAL_STATES = {"READY", "RUNNING", "PAUSED", "BLOCKED", *TERMINAL_GOAL_STATES}
EVENTS = {"GOAL_STARTED", "PROGRESS_CHECKPOINT", "QUESTION", "DECISION_REQUIRED", "BLOCKED", "COMPLETE", "FAILED", "CANCELLED"}
INSPECTION_MODES = {"result_only", "checkpoints", "conversational", "deep_inspection", "live_supervision"}
ROUTING_DIMENSIONS = {"account", "runtime", "project", "archetype", "task_taxonomy", "time_window"}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _now() -> float:
    return time.time()


def _require_goal(store: Store, goal_id: str):
    row = store.db.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown goal: {goal_id}")
    return row


def create_goal(store: Store, goal_id: str, title: str, owner: str, specialist_id: str | None = None,
                acceptance_criteria: list[str] | None = None, inspection_mode: str = "result_only",
                checkpoint_policy: dict | None = None, worker_profile: dict | None = None,
                worker_pack: str | None = None, permissions: list[str] | None = None,
                worker_class: str = "basic") -> dict:
    if not goal_id or not title or not owner:
        raise ValueError("goal_id, title and owner are required")
    if inspection_mode not in INSPECTION_MODES:
        raise ValueError("invalid inspection mode")
    if worker_class not in {"basic", "specialist"}:
        raise ValueError("invalid worker class")
    if worker_class == "basic" and specialist_id:
        raise ValueError("basic worker cannot have specialist identity")
    if store.db.execute("SELECT 1 FROM goals WHERE id=?", (goal_id,)).fetchone():
        raise ValueError("goal already exists")
    criteria = acceptance_criteria or []; policy = checkpoint_policy or {}; profile = worker_profile or {}; permissions = permissions or []
    if any(not isinstance(item, str) for item in permissions): raise ValueError("permissions must be strings")
    if {"git.push", "external.publish", "provider.paid_submit", "destructive.delete"}.intersection(permissions):
        raise PermissionError("goal creation cannot grant destructive/external authority")
    stamp = _now(); body = {"id": goal_id, "title": title, "owner": owner, "specialist_id": specialist_id,
                             "acceptance": criteria, "inspection_mode": inspection_mode, "checkpoint_policy": policy, "worker_profile": profile, "worker_pack": worker_pack, "permissions": permissions, "worker_class": worker_class}
    store.db.execute("INSERT INTO goals(id,title,status,specialist_id,inspection_mode,acceptance_json,checkpoint_policy_json,worker_profile_json,worker_pack_id,permissions_json,worker_class,digest,source,owner,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (goal_id, title, "READY", specialist_id, inspection_mode, json.dumps(criteria), json.dumps(policy, sort_keys=True), json.dumps(profile, sort_keys=True), worker_pack, json.dumps(permissions, sort_keys=True), worker_class, _digest(body), "master", owner, stamp, stamp))
    store.db.execute("INSERT INTO goal_events(id,goal_id,kind,payload_json,sequence,created_at) VALUES(?,?,?,?,?,?)",
                     (uuid.uuid4().hex, goal_id, "GOAL_STARTED", json.dumps({"status": "READY"}), 1, stamp))
    store.db.commit()
    return dict(_require_goal(store, goal_id))


def append_goal_event(store: Store, goal_id: str, kind: str, payload: dict, actor: str) -> dict:
    goal = _require_goal(store, goal_id)
    if kind not in EVENTS:
        raise ValueError("invalid goal event")
    if goal["status"] in TERMINAL_GOAL_STATES:
        raise ValueError("terminal goal rejects late events")
    allowed = {
        "READY": {"GOAL_STARTED", "CANCELLED"},
        "RUNNING": {"PROGRESS_CHECKPOINT", "QUESTION", "DECISION_REQUIRED", "BLOCKED", "COMPLETE", "FAILED", "CANCELLED", "GOAL_STARTED"},
        "PAUSED": {"GOAL_STARTED", "CANCELLED"},
        "BLOCKED": {"GOAL_STARTED", "PROGRESS_CHECKPOINT", "QUESTION", "CANCELLED"},
    }
    if kind not in allowed.get(goal["status"], set()):
        raise ValueError(f"invalid goal transition {goal['status']} -> {kind}")
    if kind in {"COMPLETE", "FAILED", "CANCELLED"} and not actor:
        raise PermissionError("terminal event requires supervisor actor")
    sequence = store.db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM goal_events WHERE goal_id=?", (goal_id,)).fetchone()[0]
    state = {"GOAL_STARTED": "RUNNING", "BLOCKED": "BLOCKED", "COMPLETE": "COMPLETE", "FAILED": "FAILED", "CANCELLED": "CANCELLED"}.get(kind)
    if state:
        store.db.execute("UPDATE goals SET status=?,updated_at=? WHERE id=?", (state, _now(), goal_id))
    row = {"id": uuid.uuid4().hex, "goal_id": goal_id, "kind": kind, "payload": payload, "sequence": sequence, "actor": actor}
    store.db.execute("INSERT INTO goal_events(id,goal_id,kind,payload_json,sequence,created_at) VALUES(?,?,?,?,?,?)",
                     (row["id"], goal_id, kind, json.dumps({"payload": payload, "actor": actor}, sort_keys=True), sequence, _now()))
    store.db.commit(); return row


def checkpoint(store: Store, goal_id: str, state: dict, evidence: list[str], next_action: str,
               recoverability: str, owner: str) -> dict:
    goal = _require_goal(store, goal_id)
    if goal["status"] in TERMINAL_GOAL_STATES:
        raise ValueError("terminal goal cannot checkpoint")
    sequence = store.db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM checkpoints WHERE goal_id=?", (goal_id,)).fetchone()[0]
    body = {"goal_id": goal_id, "sequence": sequence, "state": state, "evidence": evidence,
            "next_action": next_action, "recoverability": recoverability}
    row = (uuid.uuid4().hex, goal_id, sequence, json.dumps(state, sort_keys=True), json.dumps(evidence), next_action,
           recoverability, _digest(body), "service", owner, _now())
    store.db.execute("INSERT INTO checkpoints(id,goal_id,sequence,state_json,evidence_json,next_action,recoverability,digest,source,owner,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", row)
    store.db.commit()
    append_goal_event(store, goal_id, "PROGRESS_CHECKPOINT", {"checkpoint_id": row[0], "digest": row[7]}, owner)
    return {"id": row[0], "goal_id": goal_id, "sequence": sequence, "digest": row[7]}


def set_inspection_mode(store: Store, goal_id: str, mode: str, actor: str) -> None:
    if mode not in INSPECTION_MODES: raise ValueError("invalid inspection mode")
    if actor != "master": raise PermissionError("only Master may elevate inspection")
    goal = _require_goal(store, goal_id)
    previous = goal["inspection_mode"]
    store.db.execute("UPDATE goals SET inspection_mode=?,updated_at=? WHERE id=?", (mode, _now(), goal_id))
    store.db.execute("INSERT INTO inspection_audit(goal_id,previous_mode,new_mode,actor,created_at) VALUES(?,?,?,?,?)",
                     (goal_id, previous, mode, actor, _now()))
    store.db.commit()


def summary(store: Store, goal_id: str, cursor: int = 0, limit: int = 20, fields: list[str] | None = None) -> dict:
    goal = dict(_require_goal(store, goal_id)); mode = goal["inspection_mode"]
    checkpoints = [dict(r) for r in store.db.execute("SELECT id,sequence,digest,next_action,recoverability,created_at FROM checkpoints WHERE goal_id=? AND sequence>? ORDER BY sequence LIMIT ?", (goal_id, cursor, min(limit, 100)))]
    task_rows = store.db.execute("SELECT id FROM tasks WHERE goal_id=? ORDER BY created_at LIMIT 100", (goal_id,)).fetchall()
    task_ids = [row["id"] for row in task_rows]
    files_changed = []
    tests = []
    if task_ids:
        marks = ",".join("?" for _ in task_ids)
        for manifest in store.db.execute(f"SELECT files_json FROM context_manifests WHERE run_id IN (SELECT id FROM runs WHERE task_id IN ({marks}))", task_ids):
            files_changed.extend(item.get("path") for item in json.loads(manifest["files_json"] or "[]") if item.get("path"))
        for validation in store.db.execute(f"SELECT command,exit_code FROM validations WHERE task_id IN ({marks}) ORDER BY id LIMIT 100", task_ids):
            tests.append({"command": validation["command"], "exit_code": validation["exit_code"]})
    evidence_rows = store.db.execute("SELECT id,artifact_path,kind FROM evidence_objects WHERE goal_id=? AND tombstoned_at IS NULL ORDER BY created_at DESC LIMIT 100", (goal_id,)).fetchall()
    evidence = [{"id": row["id"], "kind": row["kind"], "artifact_path": row["artifact_path"]} for row in evidence_rows]
    result = {"goal": goal, "outcome": goal["status"], "files_changed": sorted(set(files_changed))[:100], "tests": tests,
              "evidence": evidence, "evidence_ids": [item["id"] for item in evidence],
              "decisions": [],
              "unresolved_risks": [], "blockers": [], "usage": {}, "references": [row["id"] for row in checkpoints],
              "checkpoints": checkpoints, "next_cursor": checkpoints[-1]["sequence"] if checkpoints else None}
    if mode in {"deep_inspection", "live_supervision"}:
        result["events"] = [dict(r) for r in store.db.execute("SELECT * FROM goal_events WHERE goal_id=? AND sequence>? ORDER BY sequence LIMIT ?", (goal_id, cursor, min(limit, 100)))]
    else:
        result["events"] = [{"id": r["id"], "kind": r["kind"], "sequence": r["sequence"], "created_at": r["created_at"]} for r in store.db.execute("SELECT id,kind,sequence,created_at FROM goal_events WHERE goal_id=? AND sequence>? ORDER BY sequence LIMIT ?", (goal_id, cursor, min(limit, 100)))]
    if fields:
        result["goal"] = {k: result["goal"][k] for k in fields if k in result["goal"]}
    return result


def propose_memory(store: Store, goal_id: str, content: str, scope: str, source: str, owner: str, metadata: dict | None = None) -> dict:
    if _require_goal(store, goal_id)["worker_class"] != "specialist":
        raise PermissionError("basic workers cannot create durable specialist memory")
    if scope not in {"worker", "project", "skill", "organization"}: raise ValueError("invalid memory scope")
    metadata = dict(metadata or {})
    allowed = {"outcome", "reason", "confidence", "contradictory_evidence", "rejected_alternative", "accepted_alternative"}
    if set(metadata) - allowed: raise ValueError("unsupported lesson metadata")
    if metadata.get("outcome") not in {None, "accepted", "rejected", "mixed"}: raise ValueError("invalid lesson outcome")
    if metadata.get("confidence") is not None and metadata["confidence"] not in {"low", "medium", "high"}: raise ValueError("invalid lesson confidence")
    if "contradictory_evidence" in metadata and not isinstance(metadata["contradictory_evidence"], list): raise ValueError("contradictory_evidence must be a list")
    item_id = uuid.uuid4().hex; stamp = _now()
    body = {"content": content, "scope": scope, "metadata": metadata, "source": source}
    store.db.execute("INSERT INTO memory_items(id,namespace,scope,kind,content,status,metadata_json,digest,source,owner,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (item_id, f"goal:{goal_id}", scope, "candidate_lesson", content, "CANDIDATE", json.dumps(metadata, sort_keys=True), _digest(body), source, owner, stamp)); store.db.commit()
    return {"id": item_id, "status": "CANDIDATE", "metadata": metadata}


def promote_memory(store: Store, memory_id: str, target_scope: str, actor: str) -> dict:
    if actor != "master": raise PermissionError("only Master may promote memory")
    row = store.db.execute("SELECT * FROM memory_items WHERE id=?", (memory_id,)).fetchone()
    if row is None: raise ValueError("unknown memory")
    if row["status"] != "CANDIDATE": raise ValueError("memory is not promotable")
    # Memory records are immutable historical facts.  Promotion creates a
    # new revision; it must never rewrite the candidate that was proposed.
    promoted_id = uuid.uuid4().hex
    stamp = _now()
    promoted_version = int(row["version"]) + 1
    metadata = json.loads(row["metadata_json"] or "{}")
    promoted_digest = _digest({"namespace": row["namespace"], "scope": target_scope,
                               "kind": row["kind"], "content": row["content"],
                               "metadata": metadata, "version": promoted_version, "source": memory_id})
    store.db.execute("INSERT INTO memory_items(id,namespace,scope,kind,content,status,version,metadata_json,digest,source,owner,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (promoted_id, row["namespace"], target_scope, row["kind"], row["content"],
                      "PROMOTED", promoted_version, json.dumps(metadata, sort_keys=True), promoted_digest, memory_id, actor,
                      row["expires_at"], stamp))
    store.db.execute("INSERT INTO memory_promotions(id,memory_id,target_scope,decision,decided_by,created_at) VALUES(?,?,?,?,?,?)",
                     (uuid.uuid4().hex, memory_id, target_scope, "accepted", actor, stamp))
    store.db.commit()
    return {"id": promoted_id, "source_memory_id": memory_id, "status": "PROMOTED", "scope": target_scope,
            "version": promoted_version, "digest": promoted_digest}


def create_archetype(store: Store, archetype_id: str, version: int, name: str, role: str, owner: str, **kwargs) -> dict:
    if version < 1 or not archetype_id or not name or not role: raise ValueError("invalid archetype")
    if store.db.execute("SELECT 1 FROM agent_archetypes WHERE id=? AND version=?", (archetype_id, version)).fetchone(): raise ValueError("archetype version exists")
    body = {"id": archetype_id, "version": version, "name": name, "role": role, **kwargs}
    stamp = _now()
    store.db.execute("INSERT INTO agent_archetypes(id,version,name,role,taxonomy_json,skills_json,rules_json,memory_namespace,tool_policy_json,risk_class,checkpoint_policy_json,digest,source,owner,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (archetype_id, version, name, role, json.dumps(kwargs.get("taxonomy", [])), json.dumps(kwargs.get("skills", [])), json.dumps(kwargs.get("rules", [])), kwargs.get("memory_namespace", f"agent:{archetype_id}"), json.dumps(kwargs.get("tool_policy", {})), kwargs.get("risk_class", "standard"), json.dumps(kwargs.get("checkpoint_policy", {})), _digest(body), "master", owner, stamp))
    store.db.commit(); return {"id": archetype_id, "version": version, "digest": _digest(body)}


def assign_archetype(store: Store, goal_id: str, instance_id: str, archetype_id: str, version: int, actor: str) -> dict:
    if actor != "master": raise PermissionError("only Master may assign archetype")
    if _require_goal(store, goal_id)["worker_class"] != "specialist":
        raise PermissionError("basic workers cannot be assigned specialist archetypes")
    row = store.db.execute("SELECT * FROM agent_archetypes WHERE id=? AND version=? AND enabled=1", (archetype_id, version)).fetchone()
    if row is None: raise ValueError("enabled archetype version required")
    stamp = _now(); namespace = row["memory_namespace"]
    store.db.execute("INSERT OR REPLACE INTO agent_instances(id,archetype_id,archetype_version,status,memory_namespace,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (instance_id, archetype_id, version, "ACTIVE", namespace, stamp, stamp))
    store.db.execute("UPDATE goals SET specialist_id=?,archetype_id=?,archetype_version=?,updated_at=? WHERE id=?", (instance_id, archetype_id, version, stamp, goal_id)); store.db.commit()
    return {"instance_id": instance_id, "archetype_id": archetype_id, "version": version}

def inspect_archetype(store: Store, archetype_id: str, version: int | None = None) -> dict:
    row = store.db.execute("SELECT * FROM agent_archetypes WHERE id=? AND (? IS NULL OR version=?) ORDER BY version DESC LIMIT 1", (archetype_id, version, version)).fetchone()
    if row is None: raise ValueError("archetype not found")
    result = dict(row)
    for key in ("taxonomy_json", "skills_json", "rules_json", "tool_policy_json", "checkpoint_policy_json"):
        result[key[:-5].rstrip("_") if key.endswith("_json") else key] = json.loads(result.pop(key))
    return result

def set_archetype_enabled(store: Store, archetype_id: str, version: int, enabled: bool, actor: str) -> dict:
    if actor != "master": raise PermissionError("only Master may enable or disable archetypes")
    if store.db.execute("SELECT 1 FROM agent_archetypes WHERE id=? AND version=?", (archetype_id, version)).fetchone() is None:
        raise ValueError("archetype not found")
    store.db.execute("UPDATE agent_archetypes SET enabled=? WHERE id=? AND version=?", (int(enabled), archetype_id, version)); store.db.commit()
    return {"id": archetype_id, "version": version, "enabled": bool(enabled)}


def record_observation(store: Store, observation: dict) -> str:
    required = ("taxonomy", "outcome", "sample_group")
    if any(not observation.get(key) for key in required): raise ValueError("observation taxonomy/outcome/sample_group required")
    dimensions = observation.get("dimensions", {})
    if not isinstance(dimensions, dict) or set(dimensions) - ROUTING_DIMENSIONS:
        raise ValueError("unsupported routing dimension")
    if any(not isinstance(value, (str, int, float, bool)) for value in dimensions.values()):
        raise ValueError("routing dimension values must be scalar")
    numeric = ("repair_count", "elapsed_ms", "normalized_cost", "context_retention", "ui_accuracy", "code_accuracy", "logic_accuracy", "mutation_violations", "late_actions")
    for key in numeric:
        if key in observation and (isinstance(observation[key], bool) or not isinstance(observation[key], (int, float)) or observation[key] < 0):
            raise ValueError(f"invalid observation metric: {key}")
    if "failure_class" in observation and observation["failure_class"] is not None and (not isinstance(observation["failure_class"], str) or not observation["failure_class"]):
        raise ValueError("invalid observation failure class")
    payload = dict(observation)
    payload.setdefault("repair_count", 0); payload.setdefault("mutation_violations", 0); payload.setdefault("late_actions", 0)
    payload.setdefault("failure_class", None)
    oid = uuid.uuid4().hex
    store.db.execute("INSERT INTO performance_observations(id,agent_id,model_profile_id,task_id,taxonomy,outcome,payload_json,sample_group,digest,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (oid, payload.get("agent_id"), payload.get("model_profile_id"), payload.get("task_id"), payload["taxonomy"], payload["outcome"], json.dumps(payload, sort_keys=True), payload["sample_group"], _digest(payload), _now())); store.db.commit(); return oid


def recommend_model(store: Store, taxonomy: str, minimum_samples: int = 3, dimensions: dict | None = None) -> list[dict]:
    """Rank only observations matching the requested durable routing dimensions."""
    dimensions = dimensions or {}
    if set(dimensions) - ROUTING_DIMENSIONS:
        raise ValueError("unsupported routing dimension")
    observations = store.db.execute("SELECT id,model_profile_id,outcome,payload_json FROM performance_observations WHERE taxonomy=? AND model_profile_id IS NOT NULL", (taxonomy,)).fetchall()
    grouped = {}
    for observation in observations:
        payload = json.loads(observation["payload_json"] or "{}")
        observed_dimensions = payload.get("dimensions", {})
        if any(observed_dimensions.get(key) != value for key, value in dimensions.items()):
            continue
        grouped.setdefault(observation["model_profile_id"], []).append(observation)
    rows = [{"model_profile_id": profile_id, "samples": len(items),
             "successes": sum(item["outcome"] in ("accepted", "success") for item in items),
             "observation_ids": ",".join(item["id"] for item in items)}
            for profile_id, items in grouped.items() if len(items) >= minimum_samples]
    rows.sort(key=lambda row: (-row["successes"], -row["samples"], row["model_profile_id"]))
    result = []
    for row in rows:
        samples = row["samples"]; successes = row["successes"]
        confidence = "low" if samples < 5 else "medium" if samples < 20 else "high"
        profile = store.db.execute("SELECT * FROM model_profiles WHERE id=?", (row["model_profile_id"],)).fetchone()
        if profile is None: continue
        compatible = json.loads(profile["compatibility_json"])
        if any(dimensions.get(key) is not None and compatible.get(key) not in (None, dimensions[key]) for key in dimensions): continue
        baseline = json.loads(profile["baseline_json"] or "{}")
        override = json.loads(profile["routing_json"] or "{}") if "routing_json" in profile.keys() else {}
        reason = (f"taxonomy={taxonomy}; samples={samples}; uncertainty={1.0 / (samples ** 0.5):.3f}; "
                  f"cost_class={baseline.get('cost_class', 'unknown')}; compatibility=matched; "
                  f"dimensions={json.dumps(dimensions, sort_keys=True)}")
        if override:
            reason += "; administrator_override=present"
        result.append({"model_profile_id": row["model_profile_id"], "provider": profile["provider"], "runtime": profile["runtime"], "model": profile["model"], "samples": samples, "successes": successes,
                       "success_rate": successes / samples if samples else 0.0, "confidence": confidence,
                       "uncertainty": 1.0 / (samples ** 0.5) if samples else 1.0,
                       "reason": reason, "routing_override": override, "evidence_observation_ids": (row["observation_ids"] or "").split(","), "fallback": False})
    return result


def recommend_worker(store: Store, goal: dict | str, risk: str = "standard", constraints: dict | None = None) -> dict:
    """Return a model recommendation without changing the Master's selection."""
    if risk not in {"low", "standard", "high", "critical"}:
        raise ValueError("invalid worker risk")
    constraints = dict(constraints or {})
    taxonomy = goal if isinstance(goal, str) else (goal or {}).get("taxonomy") or (goal or {}).get("task_taxonomy")
    if not isinstance(taxonomy, str) or not taxonomy:
        raise ValueError("goal taxonomy is required")
    dimensions = dict(constraints.get("dimensions", {}))
    recommendations = recommend_model(store, taxonomy, int(constraints.get("minimum_samples", 3)), dimensions)
    return {"taxonomy": taxonomy, "risk": risk, "constraints": constraints,
            "recommendations": recommendations, "selected": None,
            "master_must_confirm": True}


def register_model_profile(store: Store, profile_id: str, provider: str, runtime: str, model: str, owner: str, baseline: dict, compatibility: dict, routing_override: dict | None = None) -> dict:
    if not all((profile_id, provider, runtime, model, owner)): raise ValueError("model profile identity is required")
    if not isinstance(baseline, dict) or not isinstance(compatibility, dict): raise ValueError("baseline and compatibility must be objects")
    baseline = {"strengths": [], "weaknesses": [], "context_profile": {}, "cost_class": "unknown", "autonomy_suitability": "unknown", "tool_reliability": "unknown", "task_domains": [], **baseline}
    if baseline["cost_class"] not in {"free", "low", "standard", "high", "unknown"}: raise ValueError("invalid cost class")
    if baseline["autonomy_suitability"] not in {"low", "medium", "high", "unknown"}: raise ValueError("invalid autonomy suitability")
    routing_override = dict(routing_override or {})
    body = {"id": profile_id, "provider": provider, "runtime": runtime, "model": model, "baseline": baseline, "compatibility": compatibility, "routing_override": routing_override}
    digest = _digest(body)
    store.db.execute("INSERT INTO model_profiles(id,provider,runtime,model,baseline_json,compatibility_json,routing_json,digest,source,owner,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (profile_id, provider, runtime, model, json.dumps(baseline, sort_keys=True), json.dumps(compatibility, sort_keys=True), json.dumps(routing_override, sort_keys=True), digest, "administrator", owner, _now())); store.db.commit()
    return {"id": profile_id, "digest": digest, "routing_override": routing_override}


def register_skill(store: Store, skill_id: str, version: int, content: str, provenance: str, trust_status: str, owner: str, compatible_models=None, required_tools=None, required_docs=None, discover_on_demand: bool = False) -> dict:
    if trust_status not in {"untrusted", "verified", "curated"}: raise ValueError("invalid skill trust status")
    if not provenance: raise ValueError("skill provenance is required")
    if trust_status == "curated" and not provenance.startswith("master-reviewed:"):
        raise PermissionError("curated skill requires explicit Master review provenance")
    required_docs = list(required_docs or [])
    if any(not isinstance(doc, str) or not doc for doc in required_docs): raise ValueError("required_docs must be non-empty strings")
    digest = _digest({"id": skill_id, "version": version, "content": content, "required_docs": required_docs, "discover_on_demand": bool(discover_on_demand)})
    store.db.execute("INSERT INTO skill_versions(id,version,content,digest,provenance,trust_status,compatible_models_json,required_tools_json,source,owner,created_at,required_docs_json,discover_on_demand) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (skill_id, version, content, digest, provenance, trust_status, json.dumps(compatible_models or []), json.dumps(required_tools or []), "master", owner, _now(), json.dumps(required_docs), int(discover_on_demand))); store.db.commit()
    return {"id": skill_id, "version": version, "digest": digest, "trust_status": trust_status, "required_docs": required_docs, "discover_on_demand": bool(discover_on_demand)}


def register_rule(store: Store, rule_id: str, version: int, content: str, provenance: str, trust_status: str,
                  owner: str, conflicts=None, precedence: str = "project") -> dict:
    if not all((rule_id, provenance, owner)) or version < 1: raise ValueError("rule identity is required")
    if trust_status not in {"untrusted", "verified", "curated"}: raise ValueError("invalid rule trust status")
    if trust_status == "curated" and not provenance.startswith("master-reviewed:"):
        raise PermissionError("curated rule requires explicit Master review provenance")
    if precedence not in {"organization", "project", "skill"}: raise ValueError("invalid rule precedence")
    conflicts = sorted(set(conflicts or []))
    digest = _digest({"id": rule_id, "version": version, "content": content, "conflicts": conflicts, "precedence": precedence})
    store.db.execute("INSERT INTO rule_versions(id,version,content,digest,provenance,trust_status,source,owner,created_at,conflicts_json,precedence) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (rule_id, version, content, digest, provenance, trust_status, "master", owner, _now(), json.dumps(conflicts), precedence)); store.db.commit()
    return {"id": rule_id, "version": version, "digest": digest, "conflicts": conflicts, "precedence": precedence}


def validate_worker_components(store: Store, skill_ids: list[tuple[str, int]], rule_ids: list[tuple[str, int]], capabilities: set[str], loaded_docs: set[str] | None = None, layers: dict | None = None) -> dict:
    skills = []
    loaded_docs = set(loaded_docs or ())
    for skill_id, version in skill_ids:
        row = store.db.execute("SELECT * FROM skill_versions WHERE id=? AND version=?", (skill_id, version)).fetchone()
        if row is None: raise ValueError("pinned skill version unavailable")
        required = set(json.loads(row["required_tools_json"]))
        if not required.issubset(capabilities): raise PermissionError("skill requires unavailable tools")
        required_docs = set(json.loads(row["required_docs_json"] or "[]"))
        if not required_docs.issubset(loaded_docs) and not bool(row["discover_on_demand"]):
            raise ValueError("skill required verbatim document is not loaded")
        if row["trust_status"] == "curated" and not row["provenance"].startswith("master-reviewed:"):
            raise PermissionError("curated skill has invalid review provenance")
        skills.append({"id": skill_id, "version": version, "digest": row["digest"], "required_docs": json.loads(row["required_docs_json"] or "[]"), "discover_on_demand": bool(row["discover_on_demand"])})
    for rule_id, version in rule_ids:
        rule = store.db.execute("SELECT id,version,digest,conflicts_json,precedence FROM rule_versions WHERE id=? AND version=?", (rule_id, version)).fetchone()
        if rule is None: raise ValueError("pinned rule version unavailable")
        for conflict in json.loads(rule["conflicts_json"] or "[]"):
            if any(conflict == selected[0] for selected in rule_ids):
                raise ValueError(f"rule conflict: {rule_id} conflicts with {conflict}")
    layers = dict(layers or {})
    unknown_layers = set(layers) - {"organization", "project", "skill", "tool", "authority"}
    if unknown_layers: raise ValueError("unknown composition layer")
    protected = {"provider.preflight", "filesystem.read", "git.commit", "git.integrate", "git.push", "external.publish", "destructive.delete"}
    for layer, values in layers.items():
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ValueError("composition layer values must be strings")
        if layer != "authority" and protected.intersection(values):
            raise PermissionError("non-authority layer cannot grant protected capability")
    authority = set(layers.get("authority", []))
    if not authority.issubset(set(capabilities)):
        raise PermissionError("authority layer exceeds persisted capabilities")
    # Safety and authority capabilities are never removable by a skill/rule component.
    safety = {"provider.preflight", "filesystem.read"}
    return {"skills": skills, "rules": list(rule_ids), "effective_capabilities": sorted(set(capabilities) | safety), "precedence": "safety > authority > organization > project > tool > skill", "layers": layers}


def compile_worker_pack(store: Store, pack_id: str, goal_id: str, owner: str, components: dict) -> dict:
    """Compile a digest-pinned pack; mandatory components cannot be silently omitted."""
    required = ("goal", "rules", "skills", "checkpoint", "permissions", "acceptance")
    missing = [key for key in required if key not in components]
    if missing: raise ValueError("worker pack missing mandatory components: " + ",".join(missing))
    mandatory_text = json.dumps({key: components[key] for key in required}, sort_keys=True, ensure_ascii=False)
    max_bytes = int(components.get("context_policy", {}).get("max_bytes", 1_000_000))
    if len(mandatory_text.encode("utf-8")) > max_bytes: raise ValueError("mandatory worker pack components cannot fit")
    dedup = {}; seen = set()
    for key, value in components.items():
        digest = _digest(value)
        if digest in seen: continue
        seen.add(digest); dedup[key] = {"digest": digest, "value": value, "bytes": len(json.dumps(value, ensure_ascii=False).encode("utf-8"))}
    attribution = {key: value["bytes"] for key, value in dedup.items()}
    category_aliases = {"prompt": ("prompt", "goal"), "rules": ("rules",), "skills": ("skills",),
                        "checkpoint": ("checkpoint",), "memory": ("memory", "working_memory"),
                        "system_overhead": ("system", "system_overhead")}
    cost_attribution = {category: sum(attribution.get(key, 0) for key in keys) for category, keys in category_aliases.items()}
    cost_attribution["system_overhead"] = max(cost_attribution["system_overhead"], len(json.dumps({"pack_id": pack_id, "version": 1}).encode("utf-8")))
    compiled = {"components": dedup, "attribution_bytes": attribution, "cost_attribution": cost_attribution, "budget_mode": components.get("budget_policy", {"mode": "adaptive", "warn_threshold": 0.8, "explanation_threshold": 0.95}), "mandatory_bytes": len(mandatory_text.encode("utf-8"))}
    digest = _digest(compiled); stamp = _now()
    store.db.execute("INSERT INTO worker_packs(id,goal_id,components_json,digest,version,source,owner,created_at) VALUES(?,?,?,?,?,?,?,?)",
                     (pack_id, goal_id, json.dumps(compiled, sort_keys=True, ensure_ascii=False), digest, 1, "compiler", owner, stamp)); store.db.commit()
    return {"id": pack_id, "digest": digest, "version": 1, "mandatory_bytes": compiled["mandatory_bytes"], "attribution_bytes": attribution, "cost_attribution": cost_attribution, "budget_mode": compiled["budget_mode"]}


def evaluate_budget(policy: dict | None, telemetry: dict | None) -> dict:
    """Evaluate a pack budget without silently turning adaptive policy into a hard stop."""
    policy = dict(policy or {"mode": "adaptive", "target": 1.0, "warn_threshold": 0.8, "explanation_threshold": 0.95})
    mode = policy.get("mode", "adaptive")
    if mode not in {"adaptive", "soft", "hard"}:
        raise ValueError("invalid budget mode")
    telemetry = dict(telemetry or {})
    actual = telemetry.get("normalized_cost")
    target = policy.get("target")
    if actual is None or target is None:
        if mode == "hard":
            raise ValueError("hard budget requires live normalized telemetry and target")
        return {"mode": mode, "decision": "continue", "warning": False, "explanation_required": False, "enforced": False, "reason": "telemetry unavailable; no hard stop"}
    ratio = float(actual) / float(target) if float(target) > 0 else 1.0
    warning = ratio >= float(policy.get("warn_threshold", 0.8))
    explain = ratio >= float(policy.get("explanation_threshold", 0.95))
    exceeded = ratio >= 1.0
    return {"mode": mode, "decision": "stop" if mode == "hard" and exceeded else "continue", "warning": warning, "explanation_required": explain, "enforced": mode == "hard", "ratio": ratio, "reason": "explicit hard policy" if mode == "hard" else "soft/adaptive policy"}


def record_budget_telemetry(store: Store, metrics: dict, source: str = "provider", run_id: str | None = None, task_id: str | None = None) -> dict:
    if not isinstance(metrics, dict) or not source:
        raise ValueError("telemetry metrics and source are required")
    if run_id is not None and store.db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is None:
        raise ValueError("unknown telemetry run")
    if task_id is not None and store.db.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone() is None:
        raise ValueError("unknown telemetry task")
    if run_id is not None and task_id is not None and store.db.execute("SELECT 1 FROM runs WHERE id=? AND task_id=?", (run_id, task_id)).fetchone() is None:
        raise ValueError("telemetry run/task scope mismatch")
    numeric = {"input_tokens", "cached_input_tokens", "output_tokens", "normalized_cost", "context_bytes", "tool_output_bytes", "files_read"}
    for key, value in metrics.items():
        if key in numeric and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0):
            raise ValueError(f"invalid telemetry metric: {key}")
    telemetry_id = uuid.uuid4().hex
    store.db.execute("INSERT INTO budget_telemetry(id,run_id,task_id,source,metrics_json,created_at) VALUES(?,?,?,?,?,?)",
                     (telemetry_id, run_id, task_id, source, json.dumps(metrics, sort_keys=True), _now()))
    store.db.commit()
    return {"id": telemetry_id, "run_id": run_id, "task_id": task_id, "source": source, "metrics": metrics}


def retrieve_memory(store: Store, namespace: str, query: str, limit: int = 10, requester: str = "master") -> list[dict]:
    if namespace.startswith("goal:"):
        goal = _require_goal(store, namespace[5:])
        if requester != "master" and requester not in {goal["owner"], goal["specialist_id"]}:
            raise PermissionError("memory outside requester goal scope")
    rows = store.db.execute("SELECT id,scope,kind,content,digest,version,source,created_at FROM memory_items WHERE namespace=? AND status IN ('WORKING','PROMOTED') AND tombstoned_at IS NULL AND content LIKE ? ORDER BY created_at DESC LIMIT ?", (namespace, f"%{query}%", min(limit * 5, 100))).fetchall()
    needle = query.casefold()
    ranked = []
    seen = set()
    for row in rows:
        if row["digest"] in seen: continue
        seen.add(row["digest"])
        text = row["content"].casefold()
        score = text.count(needle) if needle else 0
        item = dict(row); item.pop("created_at", None); item["relevance"] = score
        ranked.append(item)
    ranked.sort(key=lambda item: (-item["relevance"], item["id"]))
    rows = ranked[:min(limit, 50)]
    if rows:
        stamp = _now()
        store.db.execute("UPDATE memory_items SET last_accessed_at=? WHERE id IN (%s)" % ",".join("?" for _ in rows), (stamp, *[row["id"] for row in rows]))
        store.db.commit()
    return rows


def garbage_collect_memory(store: Store, now: float | None = None) -> int:
    now = now or _now()
    rows = store.db.execute("SELECT id,namespace FROM memory_items WHERE status NOT IN ('PROMOTED','PINNED') AND tombstoned_at IS NULL AND expires_at IS NOT NULL AND expires_at<=?", (now,)).fetchall()
    tombstoned = 0
    for row in rows:
        # Active goals retain their transient memory until the goal reaches a
        # terminal state; collection remains resumable through tombstones.
        goal_id = row["namespace"][5:] if row["namespace"].startswith("goal:") else None
        active = goal_id and store.db.execute("SELECT 1 FROM goals WHERE id=? AND status NOT IN ('COMPLETE','FAILED','CANCELLED')", (goal_id,)).fetchone()
        if active:
            continue
        store.db.execute("UPDATE memory_items SET tombstoned_at=?, tombstone_reason=? WHERE id=?", (now, "expired_30d", row["id"]))
        tombstoned += 1
    store.db.commit(); return tombstoned


def garbage_collect_conversation(store: Store, now: float | None = None, inactivity_days: int = 30) -> int:
    """Tombstone inactive compact conversation turns without deleting audit state."""
    now = _now() if now is None else now
    cutoff = now - inactivity_days * 86400
    rows = store.db.execute("SELECT id,task_id FROM chat_history WHERE tombstoned_at IS NULL AND COALESCE(last_accessed_at, created_at) < ?", (cutoff,)).fetchall()
    expired = 0
    for row in rows:
        active = row["task_id"] and store.db.execute("SELECT 1 FROM goals WHERE id=? AND status NOT IN ('COMPLETE','FAILED','CANCELLED')", (row["task_id"],)).fetchone()
        if active:
            continue
        store.db.execute("UPDATE chat_history SET tombstoned_at=?, tombstone_reason=? WHERE id=?", (now, "inactive_30d", row["id"]))
        expired += 1
    store.db.commit()
    return expired


def add_evidence(store: Store, evidence_id: str, goal_id: str, kind: str, metadata: dict,
                 owner: str, artifact_path: str | None = None, run_id: str | None = None, retention: str = "transient") -> dict:
    _require_goal(store, goal_id)
    if any(secret in json.dumps(metadata).lower() for secret in ("api_key", "authorization", "bearer ")):
        raise ValueError("credential-like evidence is rejected")
    body = {"goal_id": goal_id, "kind": kind, "metadata": metadata, "artifact_path": artifact_path, "run_id": run_id}
    if retention not in {"transient", "pinned", "audit"}: raise ValueError("invalid evidence retention")
    store.db.execute("INSERT INTO evidence_objects(id,goal_id,run_id,kind,metadata_json,artifact_path,digest,owner,retention,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (evidence_id, goal_id, run_id, kind, json.dumps(metadata, sort_keys=True), artifact_path, _digest(body), owner, retention, _now())); store.db.commit()
    return {"id": evidence_id, "goal_id": goal_id, "kind": kind, "digest": _digest(body)}


def set_evidence_hold(store: Store, evidence_id: str, retention: str, actor: str) -> None:
    if actor != "master": raise PermissionError("only Master may change evidence retention")
    if retention not in {"transient", "pinned", "audit"}: raise ValueError("invalid evidence retention")
    if store.db.execute("SELECT 1 FROM evidence_objects WHERE id=?", (evidence_id,)).fetchone() is None: raise ValueError("evidence not found")
    store.db.execute("UPDATE evidence_objects SET retention=?,legal_hold=? WHERE id=?", (retention, int(retention in {"pinned", "audit"}), evidence_id)); store.db.commit()


def get_evidence(store: Store, evidence_id: str, goal_id: str, requester: str, deep: bool = False, fields: list[str] | None = None, byte_limit: int = 16384) -> dict:
    audit_id = uuid.uuid4().hex
    row = store.db.execute("SELECT * FROM evidence_objects WHERE id=? AND goal_id=? AND tombstoned_at IS NULL", (evidence_id, goal_id)).fetchone()
    if row is None:
        store.db.execute("INSERT INTO evidence_access_audit VALUES(?,?,?,?,?,?,?,?)", (audit_id, evidence_id, goal_id, requester, "deep" if deep else "summary", 0, "goal scope mismatch", _now())); store.db.commit()
        raise ValueError("evidence not found in goal scope")
    goal = _require_goal(store, goal_id)
    if requester != goal["owner"] and requester != goal["specialist_id"]:
        store.db.execute("INSERT INTO evidence_access_audit VALUES(?,?,?,?,?,?,?,?)", (audit_id, evidence_id, goal_id, requester, "deep" if deep else "summary", 0, "role scope denied", _now())); store.db.commit()
        raise PermissionError("evidence outside role scope")
    result = {"id": row["id"], "kind": row["kind"], "digest": row["digest"], "artifact_path": row["artifact_path"]}
    if deep:
        if goal["inspection_mode"] not in {"deep_inspection", "live_supervision"}:
            store.db.execute("INSERT INTO evidence_access_audit VALUES(?,?,?,?,?,?,?,?)", (audit_id, evidence_id, goal_id, requester, "deep", 0, "inspection not enabled", _now())); store.db.commit()
            raise PermissionError("deep inspection not enabled")
        result["metadata"] = json.loads(row["metadata_json"])
    if fields is not None:
        allowed = {"id", "kind", "digest", "artifact_path", "metadata"}
        if set(fields) - allowed: raise ValueError("unknown evidence field")
        result = {key: result[key] for key in fields if key in result}
    if byte_limit < 128:
        raise ValueError("evidence byte_limit is too small")
    encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
    if len(encoded) > byte_limit:
        # Preserve identity/provenance references; large metadata is only
        # available through explicit artifact retrieval, never silent overflow.
        compact = {key: result[key] for key in ("id", "kind", "digest", "artifact_path") if key in result}
        compact["truncated"] = True
        if len(json.dumps(compact, ensure_ascii=False).encode("utf-8")) > byte_limit:
            raise ValueError("evidence references exceed byte_limit")
        result = compact
    accessed = _now()
    store.db.execute("INSERT INTO evidence_access_audit VALUES(?,?,?,?,?,?,?,?)", (audit_id, evidence_id, goal_id, requester, "deep" if deep else "summary", 1, "allowed", accessed))
    store.db.execute("UPDATE evidence_objects SET last_accessed_at=? WHERE id=?", (accessed, evidence_id)); store.db.commit()
    return result


def garbage_collect_evidence(store: Store, now: float | None = None, inactivity_days: int = 30) -> int:
    """Tombstone inactive transient evidence without deleting audit rows."""
    now = _now() if now is None else now
    cutoff = now - inactivity_days * 86400
    rows = store.db.execute("SELECT id FROM evidence_objects WHERE retention='transient' AND legal_hold=0 AND tombstoned_at IS NULL AND COALESCE(last_accessed_at, created_at) < ?", (cutoff,)).fetchall()
    for row in rows:
        store.db.execute("UPDATE evidence_objects SET tombstoned_at=?, tombstone_reason=? WHERE id=?", (now, "inactive_30d", row["id"]))
    store.db.commit()
    return len(rows)


def physically_collect_payloads(store: Store, now: float | None = None, grace_days: int = 7) -> dict:
    """Delete tombstoned payloads after grace while retaining audit metadata."""
    if grace_days < 0: raise ValueError("grace_days must be non-negative")
    now = _now() if now is None else now
    cutoff = now - grace_days * 86400
    artifacts = 0
    for row in store.db.execute("SELECT artifact_path FROM evidence_objects WHERE tombstoned_at IS NOT NULL AND tombstoned_at<=? AND legal_hold=0 AND artifact_path IS NOT NULL", (cutoff,)).fetchall():
        path = Path(row["artifact_path"])
        if path.is_file(): path.unlink(); artifacts += 1
    segments = store.db.execute("SELECT id FROM chat_history WHERE tombstoned_at IS NOT NULL AND tombstoned_at<=?", (cutoff,)).fetchall()
    store.db.executemany("DELETE FROM chat_history WHERE id=?", [(row["id"],) for row in segments])
    store.db.commit()
    return {"artifacts_deleted": artifacts, "conversation_segments_deleted": len(segments)}


def subscribe_goal(store: Store, goal_id: str, subscriber_id: str, event_types: list[str] | None = None) -> dict:
    _require_goal(store, goal_id)
    for kind in event_types or []:
        if kind not in EVENTS: raise ValueError("invalid subscription event")
    stamp = _now(); store.db.execute("INSERT OR REPLACE INTO goal_subscribers(goal_id,subscriber_id,event_types_json,updated_at,created_at) VALUES(?,?,?,?,COALESCE((SELECT created_at FROM goal_subscribers WHERE goal_id=? AND subscriber_id=?),?))", (goal_id, subscriber_id, json.dumps(event_types or []), stamp, goal_id, subscriber_id, stamp)); store.db.commit()
    return {"goal_id": goal_id, "subscriber_id": subscriber_id, "cursor": 0}


def poll_goal(store: Store, goal_id: str, subscriber_id: str, limit: int = 20, byte_limit: int = 16384, reconnect: bool = False) -> dict:
    row = store.db.execute("SELECT * FROM goal_subscribers WHERE goal_id=? AND subscriber_id=?", (goal_id, subscriber_id)).fetchone()
    if row is None: raise ValueError("subscription required")
    types = json.loads(row["event_types_json"]); events = []
    for event in store.db.execute("SELECT id,kind,sequence,created_at FROM goal_events WHERE goal_id=? AND sequence>? ORDER BY sequence LIMIT ?", (goal_id, row["cursor"], min(limit, 100))):
        if types and event["kind"] not in types: continue
        candidate = dict(event)
        encoded = json.dumps(candidate).encode()
        if events and sum(len(json.dumps(item).encode()) for item in events) + len(encoded) > byte_limit: break
        events.append(candidate)
    if events: store.db.execute("UPDATE goal_subscribers SET cursor=?,updated_at=? WHERE goal_id=? AND subscriber_id=?", (events[-1]["sequence"], _now(), goal_id, subscriber_id)); store.db.commit()
    latest = events[-1]["sequence"] if events else row["cursor"]
    acknowledged = store.db.execute("SELECT acknowledged_sequence FROM goal_subscribers WHERE goal_id=? AND subscriber_id=?", (goal_id, subscriber_id)).fetchone()[0]
    latest_checkpoint = None
    if reconnect:
        checkpoint = store.db.execute("SELECT id,sequence,digest,next_action,recoverability FROM checkpoints WHERE goal_id=? ORDER BY sequence DESC LIMIT 1", (goal_id,)).fetchone()
        if checkpoint:
            latest_checkpoint = dict(checkpoint)
    return {"events": events, "cursor": latest, "acknowledged_sequence": acknowledged, "latest_checkpoint": latest_checkpoint}


def acknowledge_goal(store: Store, goal_id: str, subscriber_id: str, sequence: int) -> dict:
    """Acknowledge delivered events independently from the delivery cursor."""
    row = store.db.execute("SELECT cursor,acknowledged_sequence FROM goal_subscribers WHERE goal_id=? AND subscriber_id=?", (goal_id, subscriber_id)).fetchone()
    if row is None: raise ValueError("subscription required")
    if sequence < row["acknowledged_sequence"] or sequence > row["cursor"]:
        raise ValueError("acknowledgement must be within delivered cursor")
    store.db.execute("UPDATE goal_subscribers SET acknowledged_sequence=?,updated_at=? WHERE goal_id=? AND subscriber_id=?", (sequence, _now(), goal_id, subscriber_id)); store.db.commit()
    return {"goal_id": goal_id, "subscriber_id": subscriber_id, "cursor": row["cursor"], "acknowledged_sequence": sequence}


def follow_up(store: Store, goal_id: str, conversation_id: str, message: str, resume_memory: bool = True, actor: str = "master") -> dict:
    goal = _require_goal(store, goal_id)
    if goal["status"] in TERMINAL_GOAL_STATES: raise ValueError("terminal goal cannot receive follow-up")
    if not message: raise ValueError("follow-up message required")
    latest = store.db.execute("SELECT * FROM checkpoints WHERE goal_id=? ORDER BY sequence DESC LIMIT 1", (goal_id,)).fetchone() if resume_memory else None
    checkpoint_preview = None
    if latest:
        state = json.loads(latest["state_json"] or "{}")
        checkpoint_preview = {"id": latest["id"], "sequence": latest["sequence"], "state": state,
                             "evidence": json.loads(latest["evidence_json"] or "[]")[:20], "next_action": latest["next_action"]}
    memory_refs = [dict(row) for row in store.db.execute("SELECT id,digest,version,source FROM memory_items WHERE namespace=? AND status IN ('WORKING','PROMOTED') AND tombstoned_at IS NULL ORDER BY created_at DESC LIMIT 20", (f"goal:{goal_id}",))] if resume_memory else []
    context = {"conversation_id": conversation_id, "message": message, "memory_resumed": bool(latest or memory_refs), "checkpoint_id": latest["id"] if latest else None,
               "checkpoint_preview": checkpoint_preview, "memory_refs": memory_refs}
    event = append_goal_event(store, goal_id, "QUESTION", context, actor)
    stamp = _now()
    # Persist the bounded conversation turn under the goal scope.  Raw worker
    # transcripts remain evidence-gated; this row is only the compact exchange
    # needed to reconnect the same specialist conversation after restart.
    store.db.execute("INSERT INTO chat_history(conversation_id,role,actor,content,task_id,created_at) VALUES(?,?,?,?,?,?)", (conversation_id, "user", actor, message, goal_id, stamp))
    store.db.execute("INSERT INTO chat_history(conversation_id,role,actor,content,task_id,created_at) VALUES(?,?,?,?,?,?)", (conversation_id, "assistant", goal["specialist_id"] or "specialist", json.dumps({"event_id": event["id"], "checkpoint_id": context["checkpoint_id"], "memory_refs": [item["id"] for item in memory_refs]}, sort_keys=True), goal_id, _now()))
    store.db.commit()
    return {"goal_id": goal_id, "conversation_id": conversation_id, "memory_resumed": bool(latest or memory_refs), "checkpoint_id": context["checkpoint_id"],
            "checkpoint_preview": checkpoint_preview, "memory_refs": memory_refs, "event_id": event["id"]}


def record_feedback(store: Store, goal_id: str, kind: str, content: str, actor: str = "master") -> dict:
    """Persist Master feedback without silently overwriting specialist state."""
    if actor != "master":
        raise PermissionError("only Master may provide goal feedback")
    if kind not in {"constraint", "question", "lesson", "redirect"} or not content:
        raise ValueError("feedback kind and content are required")
    goal = _require_goal(store, goal_id)
    if goal["status"] in TERMINAL_GOAL_STATES:
        raise ValueError("terminal goal rejects feedback")
    feedback_id = uuid.uuid4().hex
    store.db.execute("INSERT INTO goal_feedback(id,goal_id,kind,content,actor,created_at) VALUES(?,?,?,?,?,?)",
                     (feedback_id, goal_id, kind, content, actor, _now()))
    store.db.commit()
    event = None
    if kind == "redirect":
        event = append_goal_event(store, goal_id, "DECISION_REQUIRED", {"feedback_id": feedback_id, "content": content}, actor)
    return {"id": feedback_id, "goal_id": goal_id, "kind": kind, "event_id": event["id"] if event else None}


def resume_goal(store: Store, goal_id: str, actor: str = "master") -> dict:
    goal = _require_goal(store, goal_id)
    if goal["status"] not in {"PAUSED", "BLOCKED", "FAILED"}: raise ValueError("goal is not resumable")
    checkpoint = store.db.execute("SELECT id,state_json FROM checkpoints WHERE goal_id=? ORDER BY sequence DESC LIMIT 1", (goal_id,)).fetchone()
    if checkpoint is None:
        raise ValueError("resume requires a durable reconciliation checkpoint")
    state = json.loads(checkpoint["state_json"] or "{}")
    required = {"goal_id", "worker_id", "conversation_id", "phase", "decisions", "changed_files", "completed_tests", "pending_actions", "active_mutation", "idempotency_keys", "loaded_skill_versions", "loaded_rule_versions", "working_memory"}
    if not required.issubset(state):
        raise ValueError("resume checkpoint is missing reconciliation fields")
    return append_goal_event(store, goal_id, "GOAL_STARTED", {"resumed": True, "checkpoint_id": checkpoint["id"], "safe_to_continue": True}, actor)


def cancel_goal(store: Store, goal_id: str, reason: str, actor: str = "master") -> dict:
    if actor != "master": raise PermissionError("only Master may cancel goal")
    return append_goal_event(store, goal_id, "CANCELLED", {"reason": reason}, actor)
