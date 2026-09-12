from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import time
import threading
from pathlib import Path
from typing import Any


_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
  provider TEXT, worker_id TEXT, base_commit TEXT, goal_id TEXT, execution_json TEXT NOT NULL DEFAULT '{}', resource_versions TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS goals (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
  specialist_id TEXT, inspection_mode TEXT NOT NULL DEFAULT 'result_only',
  acceptance_json TEXT NOT NULL DEFAULT '[]', checkpoint_policy_json TEXT NOT NULL DEFAULT '{}',
  worker_profile_json TEXT NOT NULL DEFAULT '{}', worker_pack_id TEXT, permissions_json TEXT NOT NULL DEFAULT '[]',
  version INTEGER NOT NULL DEFAULT 1, digest TEXT NOT NULL, source TEXT NOT NULL,
  owner TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_archetypes (
  id TEXT NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL,
  taxonomy_json TEXT NOT NULL DEFAULT '[]', skills_json TEXT NOT NULL DEFAULT '[]', rules_json TEXT NOT NULL DEFAULT '[]',
  memory_namespace TEXT NOT NULL, tool_policy_json TEXT NOT NULL DEFAULT '{}', risk_class TEXT NOT NULL,
  checkpoint_policy_json TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 1,
  digest TEXT NOT NULL, source TEXT NOT NULL, owner TEXT NOT NULL, created_at REAL NOT NULL,
  PRIMARY KEY(id, version)
);
CREATE TRIGGER IF NOT EXISTS immutable_archetype_version
BEFORE UPDATE OF id, version, name, role, taxonomy_json, skills_json, rules_json, memory_namespace, tool_policy_json, risk_class, checkpoint_policy_json, digest, source, owner, created_at ON agent_archetypes BEGIN SELECT RAISE(ABORT, 'archetype versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_archetype_delete
BEFORE DELETE ON agent_archetypes BEGIN SELECT RAISE(ABORT, 'archetype versions are append-only'); END;
CREATE TABLE IF NOT EXISTS agent_instances (
  id TEXT PRIMARY KEY, archetype_id TEXT NOT NULL, archetype_version INTEGER NOT NULL,
  status TEXT NOT NULL, memory_namespace TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
  FOREIGN KEY(archetype_id, archetype_version) REFERENCES agent_archetypes(id, version)
);
CREATE TABLE IF NOT EXISTS model_profiles (
  id TEXT PRIMARY KEY, provider TEXT NOT NULL, runtime TEXT NOT NULL, model TEXT NOT NULL,
  baseline_json TEXT NOT NULL DEFAULT '{}', compatibility_json TEXT NOT NULL DEFAULT '{}',
  version INTEGER NOT NULL DEFAULT 1, digest TEXT NOT NULL, source TEXT NOT NULL, owner TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS immutable_model_profile_provenance
BEFORE UPDATE OF id, provider, runtime, model, baseline_json, compatibility_json, routing_json, version, digest, source, owner, created_at ON model_profiles
BEGIN SELECT RAISE(ABORT, 'model profile provenance is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_model_profile_delete
BEFORE DELETE ON model_profiles BEGIN SELECT RAISE(ABORT, 'model profiles are append-only'); END;
CREATE TABLE IF NOT EXISTS skill_versions (
  id TEXT NOT NULL, version INTEGER NOT NULL, content TEXT NOT NULL, digest TEXT NOT NULL,
  provenance TEXT NOT NULL, trust_status TEXT NOT NULL, compatible_models_json TEXT NOT NULL DEFAULT '[]',
  required_tools_json TEXT NOT NULL DEFAULT '[]', source TEXT NOT NULL, owner TEXT NOT NULL, created_at REAL NOT NULL,
  PRIMARY KEY(id, version)
);
CREATE TABLE IF NOT EXISTS rule_versions (
  id TEXT NOT NULL, version INTEGER NOT NULL, content TEXT NOT NULL, digest TEXT NOT NULL,
  provenance TEXT NOT NULL, trust_status TEXT NOT NULL, source TEXT NOT NULL, owner TEXT NOT NULL, created_at REAL NOT NULL,
  conflicts_json TEXT NOT NULL DEFAULT '[]', precedence TEXT NOT NULL DEFAULT 'project',
  PRIMARY KEY(id, version)
);
CREATE TRIGGER IF NOT EXISTS immutable_skill_version_provenance
BEFORE UPDATE OF id, version, content, digest, provenance, trust_status, compatible_models_json, required_tools_json, source, owner, created_at ON skill_versions
BEGIN SELECT RAISE(ABORT, 'skill version provenance is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_skill_version_delete
BEFORE DELETE ON skill_versions BEGIN SELECT RAISE(ABORT, 'skill versions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_rule_version_provenance
BEFORE UPDATE OF id, version, content, digest, provenance, trust_status, source, owner, created_at, conflicts_json, precedence ON rule_versions
BEGIN SELECT RAISE(ABORT, 'rule version provenance is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_rule_version_delete
BEFORE DELETE ON rule_versions BEGIN SELECT RAISE(ABORT, 'rule versions are append-only'); END;
CREATE TABLE IF NOT EXISTS worker_packs (
  id TEXT PRIMARY KEY, goal_id TEXT, archetype_id TEXT, archetype_version INTEGER,
  model_profile_id TEXT, components_json TEXT NOT NULL, digest TEXT NOT NULL, version INTEGER NOT NULL,
  source TEXT NOT NULL, owner TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS immutable_worker_pack
BEFORE UPDATE ON worker_packs BEGIN SELECT RAISE(ABORT, 'worker packs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_worker_pack_delete
BEFORE DELETE ON worker_packs BEGIN SELECT RAISE(ABORT, 'worker packs are append-only'); END;
CREATE TABLE IF NOT EXISTS memory_items (
  id TEXT PRIMARY KEY, namespace TEXT NOT NULL, scope TEXT NOT NULL, kind TEXT NOT NULL,
  content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'WORKING', version INTEGER NOT NULL DEFAULT 1,
  digest TEXT NOT NULL, source TEXT NOT NULL, owner TEXT NOT NULL, expires_at REAL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_promotions (
  id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, target_scope TEXT NOT NULL, decision TEXT NOT NULL,
  decided_by TEXT NOT NULL, created_at REAL NOT NULL, FOREIGN KEY(memory_id) REFERENCES memory_items(id)
);
CREATE TRIGGER IF NOT EXISTS immutable_memory_provenance
BEFORE UPDATE OF namespace, scope, kind, content, version, digest, source, owner, created_at ON memory_items
WHEN NEW.namespace IS NOT OLD.namespace OR NEW.scope IS NOT OLD.scope OR NEW.kind IS NOT OLD.kind
  OR NEW.content IS NOT OLD.content OR NEW.version IS NOT OLD.version OR NEW.digest IS NOT OLD.digest
  OR NEW.source IS NOT OLD.source OR NEW.owner IS NOT OLD.owner OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'memory provenance is immutable'); END;
CREATE TABLE IF NOT EXISTS performance_observations (
  id TEXT PRIMARY KEY, agent_id TEXT, model_profile_id TEXT, task_id TEXT, taxonomy TEXT NOT NULL,
  outcome TEXT NOT NULL, payload_json TEXT NOT NULL, sample_group TEXT NOT NULL, digest TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS append_only_performance_observations_update
BEFORE UPDATE ON performance_observations BEGIN SELECT RAISE(ABORT, 'performance observations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS append_only_performance_observations_delete
BEFORE DELETE ON performance_observations BEGIN SELECT RAISE(ABORT, 'performance observations are append-only'); END;
CREATE TABLE IF NOT EXISTS checkpoints (
  id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, sequence INTEGER NOT NULL, state_json TEXT NOT NULL,
  evidence_json TEXT NOT NULL, next_action TEXT NOT NULL, recoverability TEXT NOT NULL,
  digest TEXT NOT NULL, source TEXT NOT NULL, owner TEXT NOT NULL, created_at REAL NOT NULL,
  FOREIGN KEY(goal_id) REFERENCES goals(id)
);
CREATE TABLE IF NOT EXISTS goal_events (
  id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
  sequence INTEGER NOT NULL, created_at REAL NOT NULL, FOREIGN KEY(goal_id) REFERENCES goals(id)
);
CREATE TABLE IF NOT EXISTS inspection_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, goal_id TEXT NOT NULL REFERENCES goals(id),
  previous_mode TEXT NOT NULL, new_mode TEXT NOT NULL, actor TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS goal_feedback (
  id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(id),
  kind TEXT NOT NULL, content TEXT NOT NULL, actor TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_objects (
  id TEXT PRIMARY KEY, goal_id TEXT, run_id TEXT, kind TEXT NOT NULL, metadata_json TEXT NOT NULL,
  artifact_path TEXT, digest TEXT NOT NULL, owner TEXT NOT NULL, retention TEXT NOT NULL DEFAULT 'transient',
  legal_hold INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
  FOREIGN KEY(goal_id) REFERENCES goals(id)
);
CREATE TRIGGER IF NOT EXISTS immutable_evidence_provenance
BEFORE UPDATE OF id, goal_id, run_id, kind, metadata_json, artifact_path, digest, owner, created_at ON evidence_objects
BEGIN SELECT RAISE(ABORT, 'evidence provenance is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_evidence_delete
BEFORE DELETE ON evidence_objects BEGIN SELECT RAISE(ABORT, 'evidence objects are append-only'); END;
CREATE TABLE IF NOT EXISTS goal_subscribers (
  goal_id TEXT NOT NULL, subscriber_id TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
  acknowledged_sequence INTEGER NOT NULL DEFAULT 0, event_types_json TEXT NOT NULL DEFAULT '[]',
  created_at REAL NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(goal_id, subscriber_id),
  FOREIGN KEY(goal_id) REFERENCES goals(id)
);
CREATE TABLE IF NOT EXISTS api_quota_ledgers (
  id TEXT PRIMARY KEY, provider TEXT NOT NULL, account_ref TEXT NOT NULL, model TEXT NOT NULL,
  project_id TEXT NOT NULL DEFAULT '', window TEXT NOT NULL, used_json TEXT NOT NULL, limit_json TEXT NOT NULL, reset_at REAL,
  confidence TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_credentials (
  provider TEXT NOT NULL, account_ref TEXT NOT NULL, secret_ref TEXT NOT NULL,
  fingerprint TEXT NOT NULL, source TEXT NOT NULL, created_at REAL NOT NULL,
  PRIMARY KEY(provider, account_ref)
);
CREATE TABLE IF NOT EXISTS provider_route_results (
  idempotency_key TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
  result_json TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_request_events (
  id TEXT PRIMARY KEY, idempotency_key TEXT, attempt_index INTEGER NOT NULL,
  provider TEXT NOT NULL, model TEXT NOT NULL, classification TEXT NOT NULL,
  request_id TEXT, headers_json TEXT NOT NULL, usage_json TEXT NOT NULL,
  retry_after REAL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS free_routing_policy (
  id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
  providers_json TEXT NOT NULL DEFAULT '[]', owner TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_eligibility (
  provider TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
  source TEXT NOT NULL, owner TEXT NOT NULL, updated_at REAL NOT NULL,
  PRIMARY KEY(provider, model)
);
CREATE TABLE IF NOT EXISTS budget_telemetry (
  id TEXT PRIMARY KEY, run_id TEXT, task_id TEXT, source TEXT NOT NULL,
  metrics_json TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS daemon_state (
  id INTEGER PRIMARY KEY CHECK(id=1), instance_id TEXT NOT NULL, mode TEXT NOT NULL,
  status TEXT NOT NULL, last_tick REAL NOT NULL, degraded_reason TEXT
);
CREATE TABLE IF NOT EXISTS interrupted_actions (
  id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
  phase TEXT NOT NULL, classification TEXT NOT NULL, payload_json TEXT NOT NULL,
  reconciled INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL,
  FOREIGN KEY(goal_id) REFERENCES goals(id)
);
CREATE TABLE IF NOT EXISTS evidence_access_audit (
  id TEXT PRIMARY KEY, evidence_id TEXT NOT NULL, goal_id TEXT NOT NULL,
  requester TEXT NOT NULL, mode TEXT NOT NULL, allowed INTEGER NOT NULL,
  reason TEXT NOT NULL, created_at REAL NOT NULL,
  FOREIGN KEY(evidence_id) REFERENCES evidence_objects(id), FOREIGN KEY(goal_id) REFERENCES goals(id)
);
CREATE TABLE IF NOT EXISTS dependencies (
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  depends_on TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  PRIMARY KEY(task_id, depends_on)
);
CREATE TABLE IF NOT EXISTS resources (
  name TEXT PRIMARY KEY, kind TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
  paths TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS leases (
  resource TEXT NOT NULL REFERENCES resources(name) ON DELETE CASCADE,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  worker_id TEXT NOT NULL, mode TEXT NOT NULL, acquired_at REAL NOT NULL,
  expires_at REAL NOT NULL, PRIMARY KEY(resource, task_id)
);
CREATE TABLE IF NOT EXISTS task_resources (
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  resource TEXT NOT NULL REFERENCES resources(name) ON DELETE CASCADE,
  mode TEXT NOT NULL, PRIMARY KEY(task_id, resource)
);
CREATE TABLE IF NOT EXISTS workers (
  id TEXT PRIMARY KEY, provider TEXT NOT NULL, session_id TEXT, status TEXT NOT NULL,
  worktree TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  worker_id TEXT NOT NULL, provider TEXT NOT NULL, session_id TEXT,
  conversation_id TEXT, provider_session_id TEXT, resume_session_id TEXT, context_checkpoint TEXT,
  host_instance_id TEXT, owner_pid INTEGER, process_start_identity TEXT, heartbeat_deadline REAL,
  status TEXT NOT NULL, exit_code INTEGER, output TEXT NOT NULL DEFAULT '', profile_json TEXT NOT NULL DEFAULT '{}',
  requested_budget_json TEXT NOT NULL DEFAULT '{}', effective_budget_json TEXT NOT NULL DEFAULT '{}', enforcement_source TEXT, budget_mode TEXT NOT NULL DEFAULT 'soft',
  input_tokens INTEGER, cached_input_tokens INTEGER, output_tokens INTEGER, failure_class TEXT,
  output_omitted_bytes INTEGER NOT NULL DEFAULT 0, output_provenance TEXT,
  tool_output_bytes INTEGER NOT NULL DEFAULT 0, files_read INTEGER NOT NULL DEFAULT 0,
  file_bytes INTEGER NOT NULL DEFAULT 0, prompt_bytes INTEGER NOT NULL DEFAULT 0,
  model_turns INTEGER NOT NULL DEFAULT 0, wall_time_ms INTEGER NOT NULL DEFAULT 0,
  commands_executed INTEGER NOT NULL DEFAULT 0, soft_budget_exceeded INTEGER NOT NULL DEFAULT 0,
  started_at REAL NOT NULL, finished_at REAL, heartbeat_at REAL, action_count INTEGER NOT NULL DEFAULT 0,
  steering_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, task_id TEXT,
  worker_id TEXT, payload TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY, status TEXT NOT NULL, topic TEXT NOT NULL,
  decision TEXT NOT NULL, reason TEXT NOT NULL, affected_tasks TEXT NOT NULL DEFAULT '[]',
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS validations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, command TEXT NOT NULL,
  exit_code INTEGER NOT NULL, output TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS integrations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
  commit_hash TEXT NOT NULL, strategy TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
  role TEXT NOT NULL, actor TEXT NOT NULL, content TEXT NOT NULL,
  task_id TEXT, worker_id TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge (
  id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL, digest TEXT NOT NULL, loaded_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_ack (
  digest TEXT NOT NULL, actor TEXT NOT NULL, worker_id TEXT, acknowledged_at REAL NOT NULL,
  PRIMARY KEY(digest, actor, worker_id)
);
CREATE TABLE IF NOT EXISTS control_bosses (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, min_workers INTEGER NOT NULL,
  max_workers INTEGER NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS control_allocations (
  boss_id TEXT NOT NULL REFERENCES control_bosses(id) ON DELETE CASCADE,
  worker_id TEXT NOT NULL, allocated_at REAL NOT NULL,
  PRIMARY KEY(boss_id, worker_id)
);
CREATE TABLE IF NOT EXISTS control_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT, boss_id TEXT NOT NULL REFERENCES control_bosses(id) ON DELETE CASCADE,
  requested INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'WAITING', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS managed_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  worker_id TEXT NOT NULL, provider TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'QUEUED', queued_at REAL NOT NULL, started_at REAL, finished_at REAL,
  payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS context_manifests (
  run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
  files_json TEXT NOT NULL, total_bytes INTEGER NOT NULL, provenance TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS control_settings (
  id INTEGER PRIMARY KEY CHECK(id=1), policy TEXT NOT NULL DEFAULT 'shared_queue', updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS run_authority (
  run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
  mode TEXT NOT NULL, capabilities TEXT NOT NULL DEFAULT '[]', context_digest TEXT,
  context_json TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
  token_id TEXT PRIMARY KEY, capability TEXT NOT NULL, project_id TEXT NOT NULL,
  provider_id TEXT NOT NULL, job_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
  idempotency_key TEXT NOT NULL, max_attempts INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  expires_at REAL NOT NULL DEFAULT 0, invalidated INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
  run_id TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS blocker_events (
  task_id TEXT NOT NULL, blocker_key TEXT NOT NULL, external_state_version TEXT NOT NULL,
  count INTEGER NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(task_id, blocker_key)
);
CREATE TABLE IF NOT EXISTS installation_identity (
  id INTEGER PRIMARY KEY CHECK(id=1), identity_json TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS installation_identity_no_update
BEFORE UPDATE ON installation_identity BEGIN SELECT RAISE(ABORT, 'installation identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS installation_identity_no_delete
BEFORE DELETE ON installation_identity BEGIN SELECT RAISE(ABORT, 'installation identity is immutable'); END;
CREATE TABLE IF NOT EXISTS identity_observations (
  observation_id INTEGER PRIMARY KEY AUTOINCREMENT, identity_json TEXT NOT NULL, observed_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS identity_observations_no_update
BEFORE UPDATE ON identity_observations BEGIN SELECT RAISE(ABORT, 'identity observations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS identity_observations_no_delete
BEFORE DELETE ON identity_observations BEGIN SELECT RAISE(ABORT, 'identity observations are append-only'); END;
CREATE TABLE IF NOT EXISTS review_evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, payload TEXT NOT NULL, violation INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS authority_events (
  event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT NOT NULL,
  project_id TEXT NOT NULL, provider_id TEXT NOT NULL, job_id TEXT NOT NULL,
  kind TEXT NOT NULL, payload_json TEXT NOT NULL, provenance TEXT NOT NULL,
  payload_hash TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS authority_events_no_update
BEFORE UPDATE ON authority_events BEGIN SELECT RAISE(ABORT, 'authority events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS authority_events_no_delete
BEFORE DELETE ON authority_events BEGIN SELECT RAISE(ABORT, 'authority events are append-only'); END;
"""

def process_start_identity(pid: int) -> str:
    """Return a kernel process-start token where available."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
        return fields[21]
    except (OSError, IndexError):
        return f"pid:{pid}"


class Store:
    TASK_STATUSES = {"READY", "RUNNING", "REVIEW", "ACCEPTED", "DONE", "FAILED", "PAUSED",
                     "WAITING_RESOURCE", "WAITING_DEPENDENCY", "WAITING_DECISION", "STALE", "DISPUTED", "REPAIR"}
    LEASE_MODES = {"READ", "WRITE"}
    TASK_TRANSITIONS = {
        "READY": {"RUNNING", "WAITING_RESOURCE", "WAITING_DEPENDENCY", "WAITING_DECISION", "PAUSED", "FAILED", "DISPUTED", "STALE"},
        "RUNNING": {"REVIEW", "WAITING_DECISION", "FAILED", "PAUSED", "DISPUTED", "STALE"},
        "REVIEW": {"ACCEPTED", "REPAIR", "DISPUTED", "STALE", "FAILED"},
        "ACCEPTED": {"DONE"}, "DONE": set(), "FAILED": {"READY", "REPAIR", "STALE"},
        "PAUSED": {"READY", "RUNNING", "FAILED", "STALE"}, "WAITING_RESOURCE": {"READY", "RUNNING", "PAUSED", "STALE", "FAILED"},
        "WAITING_DEPENDENCY": {"READY", "RUNNING", "PAUSED", "STALE", "FAILED"}, "WAITING_DECISION": {"READY", "RUNNING", "REVIEW", "REPAIR", "PAUSED", "STALE", "FAILED"},
        "STALE": {"READY", "REPAIR", "DISPUTED", "PAUSED", "FAILED"}, "DISPUTED": {"REPAIR", "WAITING_DECISION", "PAUSED", "STALE", "FAILED"},
        "REPAIR": {"READY", "RUNNING", "PAUSED", "FAILED", "STALE"},
    }

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Scheduler workers may complete on different threads; SQLite serializes
        # short transactions while WAL improves reader/writer coexistence.
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._reconcile_lock = threading.Lock()
        # All Store connections for one durable state share the same lock;
        # per-connection locks do not protect concurrent MCP waiters.
        self._queue_lock = _path_lock(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        # Additive migration for databases created before durable job payloads.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(managed_queue)")}
        if "payload_json" not in columns:
            self.db.execute("ALTER TABLE managed_queue ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}'" )
            self.db.commit()
        evidence_columns = {row[1] for row in self.db.execute("PRAGMA table_info(evidence_objects)")}
        for name, definition in (("retention", "TEXT NOT NULL DEFAULT 'transient'"), ("legal_hold", "INTEGER NOT NULL DEFAULT 0"),
                                 ("last_accessed_at", "REAL"), ("tombstoned_at", "REAL"), ("tombstone_reason", "TEXT")):
            if name not in evidence_columns:
                self.db.execute(f"ALTER TABLE evidence_objects ADD COLUMN {name} {definition}")
        self.db.commit()
        chat_columns = {row[1] for row in self.db.execute("PRAGMA table_info(chat_history)")}
        for name, definition in (("last_accessed_at", "REAL"), ("tombstoned_at", "REAL"), ("tombstone_reason", "TEXT")):
            if name not in chat_columns:
                self.db.execute(f"ALTER TABLE chat_history ADD COLUMN {name} {definition}")
        self.db.commit()
        skill_columns = {row[1] for row in self.db.execute("PRAGMA table_info(skill_versions)")}
        for name, definition in (("required_docs_json", "TEXT NOT NULL DEFAULT '[]'"), ("discover_on_demand", "INTEGER NOT NULL DEFAULT 0")):
            if name not in skill_columns:
                self.db.execute(f"ALTER TABLE skill_versions ADD COLUMN {name} {definition}")
        self.db.commit()
        self.db.execute("""CREATE TRIGGER IF NOT EXISTS immutable_run_archetype_provenance
            BEFORE UPDATE OF archetype_id, archetype_version ON runs
            WHEN OLD.archetype_id IS NOT NULL AND (NEW.archetype_id IS NOT OLD.archetype_id OR NEW.archetype_version IS NOT OLD.archetype_version)
            BEGIN SELECT RAISE(ABORT, 'run archetype provenance is immutable'); END;""")
        self.db.commit()
        self.db.executescript("""
        CREATE TRIGGER IF NOT EXISTS append_only_checkpoints_update
        BEFORE UPDATE ON checkpoints BEGIN SELECT RAISE(ABORT, 'checkpoints are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS append_only_checkpoints_delete
        BEFORE DELETE ON checkpoints BEGIN SELECT RAISE(ABORT, 'checkpoints are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS append_only_goal_events_update
        BEFORE UPDATE ON goal_events BEGIN SELECT RAISE(ABORT, 'goal events are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS append_only_goal_events_delete
        BEFORE DELETE ON goal_events BEGIN SELECT RAISE(ABORT, 'goal events are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS task_goal_reference_insert
        BEFORE INSERT ON tasks
        WHEN NEW.goal_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM goals WHERE id=NEW.goal_id)
        BEGIN SELECT RAISE(ABORT, 'task goal reference does not exist'); END;
        CREATE TRIGGER IF NOT EXISTS task_goal_reference_update
        BEFORE UPDATE OF goal_id ON tasks
        WHEN NEW.goal_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM goals WHERE id=NEW.goal_id)
        BEGIN SELECT RAISE(ABORT, 'task goal reference does not exist'); END;
        CREATE TRIGGER IF NOT EXISTS worker_pack_goal_reference_insert
        BEFORE INSERT ON worker_packs
        WHEN NEW.goal_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM goals WHERE id=NEW.goal_id)
        BEGIN SELECT RAISE(ABORT, 'worker pack goal reference does not exist'); END;
        CREATE TRIGGER IF NOT EXISTS worker_pack_archetype_reference_insert
        BEFORE INSERT ON worker_packs
        WHEN NEW.archetype_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM agent_archetypes WHERE id=NEW.archetype_id AND version=NEW.archetype_version)
        BEGIN SELECT RAISE(ABORT, 'worker pack archetype reference does not exist'); END;
        CREATE TRIGGER IF NOT EXISTS worker_pack_scope_update
        BEFORE UPDATE OF goal_id, archetype_id, archetype_version ON worker_packs
        WHEN (NEW.goal_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM goals WHERE id=NEW.goal_id))
          OR (NEW.archetype_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM agent_archetypes WHERE id=NEW.archetype_id AND version=NEW.archetype_version))
        BEGIN SELECT RAISE(ABORT, 'worker pack scope reference does not exist'); END;
        CREATE TRIGGER IF NOT EXISTS performance_task_reference_insert
        BEFORE INSERT ON performance_observations
        WHEN NEW.task_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM tasks WHERE id=NEW.task_id)
        BEGIN SELECT RAISE(ABORT, 'performance task reference does not exist'); END;
        CREATE TRIGGER IF NOT EXISTS performance_model_reference_insert
        BEFORE INSERT ON performance_observations
        WHEN NEW.model_profile_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM model_profiles WHERE id=NEW.model_profile_id)
        BEGIN SELECT RAISE(ABORT, 'performance model reference does not exist'); END;
        CREATE TRIGGER IF NOT EXISTS performance_scope_update
        BEFORE UPDATE OF task_id, model_profile_id ON performance_observations
        WHEN (NEW.task_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM tasks WHERE id=NEW.task_id))
          OR (NEW.model_profile_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM model_profiles WHERE id=NEW.model_profile_id))
        BEGIN SELECT RAISE(ABORT, 'performance scope reference does not exist'); END;
        CREATE TRIGGER IF NOT EXISTS evidence_goal_reference_insert
        BEFORE INSERT ON evidence_objects
        WHEN NEW.goal_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM goals WHERE id=NEW.goal_id)
        BEGIN SELECT RAISE(ABORT, 'evidence goal reference does not exist'); END;
        CREATE TRIGGER IF NOT EXISTS evidence_run_reference_insert
        BEFORE INSERT ON evidence_objects
        WHEN NEW.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM runs WHERE id=NEW.run_id)
        BEGIN SELECT RAISE(ABORT, 'evidence run reference does not exist'); END;
        CREATE TRIGGER IF NOT EXISTS evidence_run_goal_scope_insert
        BEFORE INSERT ON evidence_objects
        WHEN NEW.run_id IS NOT NULL AND NEW.goal_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM runs r JOIN tasks t ON t.id=r.task_id WHERE r.id=NEW.run_id AND t.goal_id=NEW.goal_id)
        BEGIN SELECT RAISE(ABORT, 'evidence run and goal scope mismatch'); END;
        CREATE TRIGGER IF NOT EXISTS evidence_scope_update
        BEFORE UPDATE OF goal_id, run_id ON evidence_objects
        WHEN (NEW.goal_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM goals WHERE id=NEW.goal_id))
          OR (NEW.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM runs WHERE id=NEW.run_id))
          OR (NEW.run_id IS NOT NULL AND NEW.goal_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM runs r JOIN tasks t ON t.id=r.task_id WHERE r.id=NEW.run_id AND t.goal_id=NEW.goal_id))
        BEGIN SELECT RAISE(ABORT, 'evidence scope reference does not exist'); END;
        """)
        self.db.commit()
        model_columns = {row[1] for row in self.db.execute("PRAGMA table_info(model_profiles)")}
        if "routing_json" not in model_columns:
            self.db.execute("ALTER TABLE model_profiles ADD COLUMN routing_json TEXT NOT NULL DEFAULT '{}'")
        self.db.commit()
        goal_columns = {row[1] for row in self.db.execute("PRAGMA table_info(goals)")}
        for name, definition in (("worker_profile_json", "TEXT NOT NULL DEFAULT '{}'"), ("worker_pack_id", "TEXT"), ("permissions_json", "TEXT NOT NULL DEFAULT '[]'"), ("archetype_id", "TEXT"), ("archetype_version", "INTEGER"), ("worker_class", "TEXT NOT NULL DEFAULT 'basic'")):
            if name not in goal_columns:
                self.db.execute(f"ALTER TABLE goals ADD COLUMN {name} {definition}")
        self.db.execute("UPDATE goals SET worker_class='specialist' WHERE worker_class='basic' AND (specialist_id IS NOT NULL OR archetype_id IS NOT NULL)")
        self.db.commit()
        memory_columns = {row[1] for row in self.db.execute("PRAGMA table_info(memory_items)")}
        for name, definition in (("metadata_json", "TEXT NOT NULL DEFAULT '{}'"), ("last_accessed_at", "REAL"), ("tombstoned_at", "REAL"), ("tombstone_reason", "TEXT")):
            if name not in memory_columns:
                self.db.execute(f"ALTER TABLE memory_items ADD COLUMN {name} {definition}")
        self.db.commit()
        rule_columns = {row[1] for row in self.db.execute("PRAGMA table_info(rule_versions)")}
        for name, definition in (("conflicts_json", "TEXT NOT NULL DEFAULT '[]'"), ("precedence", "TEXT NOT NULL DEFAULT 'project'")):
            if name not in rule_columns:
                self.db.execute(f"ALTER TABLE rule_versions ADD COLUMN {name} {definition}")
        self.db.commit()
        # Additive migrations keep existing SQLite state usable.
        task_columns = {row[1] for row in self.db.execute("PRAGMA table_info(tasks)")}
        for name, definition in (("execution_json", "TEXT NOT NULL DEFAULT '{}'"), ("goal_id", "TEXT")):
            if name not in task_columns:
                self.db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(runs)")}
        for name, definition in (("conversation_id", "TEXT"), ("provider_session_id", "TEXT"), ("resume_session_id", "TEXT"), ("context_checkpoint", "TEXT"),
                                 ("host_instance_id", "TEXT"), ("owner_pid", "INTEGER"), ("process_start_identity", "TEXT"), ("heartbeat_deadline", "REAL"),
                                 ("requested_budget_json", "TEXT NOT NULL DEFAULT '{}'"), ("effective_budget_json", "TEXT NOT NULL DEFAULT '{}'"), ("enforcement_source", "TEXT"), ("budget_mode", "TEXT NOT NULL DEFAULT 'soft'"),
                                 ("profile_json", "TEXT NOT NULL DEFAULT '{}'"), ("input_tokens", "INTEGER"),
                                 ("cached_input_tokens", "INTEGER"), ("output_tokens", "INTEGER"), ("failure_class", "TEXT"),
                                 ("output_omitted_bytes", "INTEGER NOT NULL DEFAULT 0"), ("output_provenance", "TEXT"),
                                 ("tool_output_bytes", "INTEGER NOT NULL DEFAULT 0"), ("files_read", "INTEGER NOT NULL DEFAULT 0"),
                                 ("file_bytes", "INTEGER NOT NULL DEFAULT 0"), ("prompt_bytes", "INTEGER NOT NULL DEFAULT 0"),
                                 ("model_turns", "INTEGER NOT NULL DEFAULT 0"), ("wall_time_ms", "INTEGER NOT NULL DEFAULT 0"),
                                 ("archetype_id", "TEXT"), ("archetype_version", "INTEGER"),
                                 ("commands_executed", "INTEGER NOT NULL DEFAULT 0"), ("soft_budget_exceeded", "INTEGER NOT NULL DEFAULT 0"),
                                 ("heartbeat_at", "REAL"), ("action_count", "INTEGER NOT NULL DEFAULT 0"), ("steering_json", "TEXT NOT NULL DEFAULT '[]'")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
        approval_columns = {row[1] for row in self.db.execute("PRAGMA table_info(approvals)")}
        for name in ("run_id", "task_id"):
            if name not in approval_columns:
                self.db.execute(f"ALTER TABLE approvals ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        self.db.commit()
        quota_columns = {row[1] for row in self.db.execute("PRAGMA table_info(api_quota_ledgers)")}
        if "project_id" not in quota_columns:
            self.db.execute("ALTER TABLE api_quota_ledgers ADD COLUMN project_id TEXT NOT NULL DEFAULT ''")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def control_register_boss(self, boss_id: str, name: str | None = None, min_workers: int = 1, max_workers: int | None = None) -> None:
        if min_workers < 1 or (max_workers is not None and max_workers < min_workers): raise ValueError("invalid worker bounds")
        self.db.execute("INSERT OR REPLACE INTO control_bosses(id,name,min_workers,max_workers,created_at) VALUES(?,?,?,?,?)", (boss_id, name or boss_id, min_workers, max_workers or min_workers, self._now())); self.db.commit()

    def control_set_policy(self, policy: str) -> None:
        if policy not in {"lock", "flexible"}: raise ValueError("invalid control policy")
        self.db.execute("INSERT OR REPLACE INTO control_settings(id,policy,updated_at) VALUES(1,?,?)", (policy, self._now()))
        config = self.path.parent / "config.json"
        if config.exists():
            data = json.loads(config.read_text()); data.setdefault("control", {})["policy"] = policy; config.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        self.db.commit()

    def control_request_workers(self, boss_id: str, requested: int | None = None) -> dict[str, Any]:
        boss = self.db.execute("SELECT * FROM control_bosses WHERE id=?", (boss_id,)).fetchone()
        if not boss: raise ValueError(f"unknown boss: {boss_id}")
        count = requested or boss["min_workers"]
        if count < boss["min_workers"] or count > boss["max_workers"]: raise ValueError("requested workers outside boss bounds")
        policy_row = self.db.execute("SELECT policy FROM control_settings WHERE id=1").fetchone()
        policy = policy_row[0] if policy_row else None
        if not policy:
            config_path = self.path.parent / "config.json"
            policy = json.loads(config_path.read_text()).get("control", {}).get("policy", "lock") if config_path.exists() else "lock"
        used = {r[0] for r in self.db.execute("SELECT worker_id FROM control_allocations")}
        config_path = self.path.parent / "config.json"
        config_data = json.loads(config_path.read_text()) if config_path.exists() else {}
        configured = config_data.get("workers", [])
        # Flexible may grow the configured pool when the request itself asks
        # for more workers than currently exist. Lock never mutates capacity.
        if policy == "flexible" and count > len(configured):
            providers = config_data.get("providers", [])
            default_provider = providers[0] if providers else (configured[0].get("provider", "") if configured else "")
            existing_ids = {w.get("id") for w in configured}
            for index in range(1, count + 1):
                worker_id = f"worker-{index}"
                if worker_id in existing_ids:
                    continue
                configured.append({"id": worker_id, "provider": default_provider, "role": "general", "scope": "project"})
                existing_ids.add(worker_id)
            config_data["workers"] = configured
            config_path.write_text(json.dumps(config_data, ensure_ascii=False, indent=2) + "\n")
        available = [w["id"] for w in configured if w.get("id") not in used and (policy != "lock" or not w.get("boss_id") or w.get("boss_id") == boss_id)]
        if len(available) < count:
            if policy == "lock":
                return {"status": "PENDING", "reason": "fixed worker capacity is exhausted; no additional worker will be assigned", "requested": count, "available": len(available), "worker_ids": []}
            existing = self.db.execute("SELECT id FROM control_queue WHERE boss_id=? AND requested=? AND status='WAITING' ORDER BY id LIMIT 1", (boss_id, count)).fetchone()
            if existing:
                pos = self.db.execute("SELECT COUNT(*) FROM control_queue WHERE status='WAITING' AND id<=?", (existing[0],)).fetchone()[0]
                return {"status": "WAITING", "requested": count, "available": len(available), "queue_position": pos, "worker_ids": []}
            cur = self.db.execute("INSERT INTO control_queue(boss_id,requested,created_at) VALUES(?,?,?)", (boss_id, count, self._now())); self.db.commit()
            pos = self.db.execute("SELECT COUNT(*) FROM control_queue WHERE status='WAITING' AND id<=?", (cur.lastrowid,)).fetchone()[0]
            return {"status": "WAITING", "requested": count, "available": len(available), "queue_position": pos, "worker_ids": []}
        chosen = available[:count]
        self.db.executemany("INSERT INTO control_allocations(boss_id,worker_id,allocated_at) VALUES(?,?,?)", [(boss_id, w, self._now()) for w in chosen]); self.db.commit()
        return {"status": "ALLOCATED", "requested": count, "available": len(available), "worker_ids": chosen}

    def control_release_boss(self, boss_id: str) -> dict[str, Any]:
        self.db.execute("DELETE FROM control_allocations WHERE boss_id=?", (boss_id,)); self.db.execute("UPDATE control_queue SET status='RELEASED' WHERE boss_id=? AND status='WAITING'", (boss_id,)); self.db.commit(); return self.control_promote_queue()

    def control_promote_queue(self) -> dict[str, Any]:
        promoted = []
        rows = self.db.execute("SELECT * FROM control_queue WHERE status='WAITING' ORDER BY id").fetchall()
        for row in rows:
            result = self.control_request_workers(row["boss_id"], row["requested"])
            if result["status"] != "ALLOCATED": break
            self.db.execute("UPDATE control_queue SET status='ALLOCATED' WHERE id=?", (row["id"],)); promoted.append({"boss_id": row["boss_id"], "worker_ids": result["worker_ids"]})
        self.db.commit(); return {"promoted": promoted}

    def control_snapshot(self) -> dict[str, Any]:
        policy = self.db.execute("SELECT policy FROM control_settings WHERE id=1").fetchone()
        return {"policy": policy[0] if policy else "lock", "bosses": [dict(x) for x in self.db.execute("SELECT * FROM control_bosses ORDER BY id")], "allocations": [dict(x) for x in self.db.execute("SELECT * FROM control_allocations ORDER BY allocated_at")], "queue": [dict(x) for x in self.db.execute("SELECT * FROM control_queue WHERE status='WAITING' ORDER BY id")]}

    def record_history(self, conversation_id: str, role: str, actor: str,
                       content: str, task_id: str | None = None,
                       worker_id: str | None = None) -> None:
        if not conversation_id or not content:
            raise ValueError("conversation_id and content are required")
        self.db.execute("INSERT INTO chat_history(conversation_id,role,actor,content,task_id,worker_id,created_at) VALUES(?,?,?,?,?,?,?)",
                        (conversation_id, role, actor, content, task_id, worker_id, self._now()))
        self.db.commit()

    def history(self, conversation_id: str, limit: int = 50):
        return self.db.execute("SELECT * FROM chat_history WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
                               (conversation_id, limit)).fetchall()

    def load_knowledge(self, path: str | Path) -> str:
        source = Path(path).resolve()
        if not source.is_file(): raise ValueError(f"knowledge file not found: {source}")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        self.db.execute("INSERT INTO knowledge(path,digest,loaded_at) VALUES(?,?,?)", (str(source), digest, self._now()))
        self.db.commit(); return digest

    def acknowledge_knowledge(self, actor: str, worker_id: str | None = None) -> None:
        row = self.db.execute("SELECT digest FROM knowledge ORDER BY id DESC LIMIT 1").fetchone()
        if row is None: raise ValueError("load knowledge before acknowledgement")
        self.db.execute("INSERT OR REPLACE INTO knowledge_ack(digest,actor,worker_id,acknowledged_at) VALUES(?,?,?,?)", (row["digest"], actor, worker_id, self._now()))
        self.db.commit()

    def knowledge_ready(self, worker_id: str) -> bool:
        config = self.path.parent / "config.json"
        if not config.exists() or not json.loads(config.read_text()).get("boss", {}).get("knowledge_required", False): return True
        row = self.db.execute("SELECT digest FROM knowledge ORDER BY id DESC LIMIT 1").fetchone()
        if row is None: return False
        digest = row["digest"]
        boss = self.db.execute("SELECT 1 FROM knowledge_ack WHERE digest=? AND actor='boss'", (digest,)).fetchone()
        worker = self.db.execute("SELECT 1 FROM knowledge_ack WHERE digest=? AND actor='worker' AND worker_id=?", (digest, worker_id)).fetchone()
        return boss is not None and worker is not None

    def knowledge_context(self, mode: str = "summary") -> str:
        if mode == "none": return ""
        row = self.db.execute("SELECT path,digest FROM knowledge ORDER BY id DESC LIMIT 1").fetchone()
        if row is None: return ""
        source = Path(row["path"])
        if not source.is_file(): return ""
        content = source.read_text()
        if mode == "summary": content = "\n".join(line for line in content.splitlines() if line.strip())[:4000]
        return f"KNOWLEDGE DIGEST: {row['digest']}\nKNOWLEDGE CONTENT:\n{content}"

    def _now(self) -> float:
        return time.time()

    def add_task(self, task_id: str, title: str, provider: str | None = None, depends_on=(), execution=None, goal_id: str | None = None) -> None:
        depends_on = list(depends_on)
        if not task_id or not title:
            raise ValueError("task id and title are required")
        if self.task(task_id) is not None:
            raise ValueError(f"task already exists: {task_id}")
        if task_id in depends_on or len(depends_on) != len(set(depends_on)):
            raise ValueError("task dependency graph cannot contain self/duplicate dependencies")
        missing = [dep for dep in depends_on if self.task(dep) is None]
        if missing:
            raise ValueError(f"unknown task dependencies: {', '.join(missing)}")
        now = self._now()
        if goal_id is not None and not self.db.execute("SELECT 1 FROM goals WHERE id=?", (goal_id,)).fetchone():
            raise ValueError("unknown goal")
        self.db.execute("INSERT INTO tasks(id,title,status,provider,goal_id,execution_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (task_id, title, "READY", provider, goal_id, json.dumps(execution or {}, sort_keys=True), now, now))
        for dep in depends_on:
            self.db.execute("INSERT INTO dependencies(task_id,depends_on) VALUES(?,?)", (task_id, dep))
        self.db.commit()

    def record_run_archetype(self, run_id: str) -> None:
        """Derive run archetype provenance from persisted task/goal identity."""
        self.db.execute("UPDATE runs SET archetype_id=(SELECT g.archetype_id FROM tasks t JOIN goals g ON g.id=t.goal_id WHERE t.id=runs.task_id), archetype_version=(SELECT g.archetype_version FROM tasks t JOIN goals g ON g.id=t.goal_id WHERE t.id=runs.task_id) WHERE id=?", (run_id,))
        self.db.commit()

    def task(self, task_id: str):
        return self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    def tasks(self):
        return self.db.execute("SELECT * FROM tasks ORDER BY created_at").fetchall()

    def set_task_status(self, task_id: str, status: str) -> None:
        with self._queue_lock:
            if status not in self.TASK_STATUSES:
                raise ValueError(f"invalid task status: {status}")
            current = self.task(task_id)
            if current is None:
                raise ValueError(f"unknown task: {task_id}")
            if status != current["status"] and status not in self.TASK_TRANSITIONS[current["status"]]:
                raise ValueError(f"invalid task transition: {current['status']} -> {status}")
            self.db.execute("UPDATE tasks SET status=?,updated_at=? WHERE id=?", (status, self._now(), task_id))
            self.db.commit()

    def settle_task_result(self, task_id: str, complete: bool) -> str:
        """Atomically settle provider completion without overriding a decision checkpoint."""
        with self._queue_lock:
            current = self.task(task_id)
            if current is None:
                raise ValueError(f"unknown task: {task_id}")
            status = current["status"]
            if status == "WAITING_DECISION":
                return status
            target = "REVIEW" if complete else "FAILED"
            if status == target:
                return status
            if target not in self.TASK_TRANSITIONS.get(status, set()):
                raise ValueError(f"invalid task transition: {status} -> {target}")
            self.db.execute("UPDATE tasks SET status=?,updated_at=? WHERE id=?", (target, self._now(), task_id))
            self.db.commit()
            return target

    def claim_task(self, task_id: str, worker_id: str, base_commit: str | None = None) -> None:
        """Atomically claim a runnable task so duplicate dispatch cannot execute it."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            task = self.task(task_id)
            if task is None:
                raise ValueError(f"unknown task: {task_id}")
            if task["status"] not in ("READY", "REPAIR", "WAITING_RESOURCE"):
                raise ValueError(f"task {task_id} is not claimable from {task['status']}")
            if not self.knowledge_ready(worker_id):
                self.db.rollback(); self.set_task_status(task_id, "WAITING_DECISION")
                raise RuntimeError("knowledge acknowledgement required for boss and worker")
            if not self.dependencies_ready(task_id):
                self.db.rollback()
                self.set_task_status(task_id, "WAITING_DEPENDENCY")
                raise RuntimeError(f"dependencies are not ready for {task_id}")
            self.db.execute("UPDATE tasks SET worker_id=?,base_commit=COALESCE(?,base_commit),status='RUNNING',updated_at=? WHERE id=?",
                            (worker_id, base_commit, self._now(), task_id))
            self.db.commit()
        except Exception:
            if self.db.in_transaction:
                self.db.rollback()
            raise

    def accept_task(self, task_id: str) -> None:
        task = self.task(task_id)
        if task is None or task["status"] != "REVIEW":
            raise ValueError("only REVIEW tasks can be accepted")
        passed = self.db.execute(
            "SELECT 1 FROM validations WHERE task_id=? AND exit_code=0 AND created_at>? LIMIT 1",
            (task_id, task["updated_at"]),
        ).fetchone()
        if passed is None:
            raise ValueError("at least one passing validation is required before acceptance")
        latest = self.latest_run(task_id)
        if latest and self.authority_snapshot(latest["id"]):
            evidence = self.review_evidence(task_id)
            if evidence:
                raise ValueError("required review evidence missing: " + ", ".join(evidence))
        self.set_task_status(task_id, "ACCEPTED")

    def review_evidence(self, task_id: str) -> list[str]:
        """Return deterministic missing evidence for an authority-managed review."""
        latest = self.latest_run(task_id)
        if not latest: return ["run"]
        authority = self.authority_snapshot(latest["id"])
        missing = []
        if not authority: missing.append("authority policy")
        elif not authority.get("context_digest"): missing.append("context packet digest")
        if latest["output"] == "": missing.append("worker output")
        passed = self.db.execute("SELECT 1 FROM validations WHERE task_id=? AND exit_code=0 AND created_at>? LIMIT 1", (task_id, latest["started_at"])).fetchone()
        if passed is None: missing.append("passing validation")
        events = self.authority_events(latest["id"], task_id)
        missing.extend(self._validate_authority_events(latest, events))
        event_kinds = {event["kind"] for event in events}
        for required in ("policy_resolved", "provider_result", "validation_result"):
            if required not in event_kinds: missing.append(required + " event")
        if any(event["provenance"] not in {"provider", "supervisor", "runtime_adapter", "store"} for event in events):
            missing.append("trusted event provenance")
        expected_project = self.project_id()
        if any(event["run_id"] != latest["id"] or event["task_id"] != task_id or
               event["provider_id"] != latest["provider"] or event["job_id"] != latest["id"] or
               event["project_id"] != expected_project for event in events):
            missing.append("exact event scope")
        if any(event["kind"] == "policy_violation" for event in events): missing.append("recorded policy violation")
        return missing

    def _validate_authority_events(self, run, events) -> list[str]:
        from .authority import digest
        errors = []
        terminal = []
        for event in events:
            try: payload = json.loads(event["payload_json"])
            except (TypeError, json.JSONDecodeError): errors.append("invalid event payload"); continue
            if digest(payload) != event["payload_hash"]: errors.append("event payload hash mismatch")
            required = {
                "policy_resolved": ("policy", "context_digest"),
                "provider_result": ("status", "exit_code", "output_bytes", "usage"),
                "validation_result": ("validation_id", "run_id", "command", "exit_code", "output_bytes"),
                "lease_acquired": ("resource", "mode", "worker_id"),
                "approval_consumed": ("token_id", "capability", "project_id", "provider_id", "job_id", "run_id", "task_id", "idempotency_key", "payload_hash"),
                "runtime_mutation_granted": ("capability", "operation", "resource"),
                "runtime_mutation_denied": ("capability", "reason", "resource"),
            }.get(event["kind"], ())
            if any(key not in payload for key in required): errors.append("invalid " + event["kind"] + " schema")
            if event["kind"] == "provider_result": terminal.append((payload.get("status"), payload.get("exit_code")))
            if event["kind"] == "validation_result":
                row = self.db.execute("SELECT * FROM validations WHERE id=? AND task_id=?", (payload.get("validation_id"), run["task_id"])).fetchone()
                if (not row or payload.get("run_id") != run["id"] or row["command"] != payload.get("command") or
                    row["exit_code"] != payload.get("exit_code") or len(row["output"].encode("utf-8")) != payload.get("output_bytes")):
                    errors.append("validation event conflicts with durable validation row")
            if event["kind"] == "approval_consumed":
                approval = self.db.execute("SELECT * FROM approvals WHERE token_id=?", (payload.get("token_id"),)).fetchone()
                if not approval or any(payload.get(key) != approval[key] for key in ("capability", "project_id", "provider_id", "job_id", "run_id", "task_id", "idempotency_key", "payload_hash")):
                    errors.append("approval event scope conflicts with durable approval")
            if event["kind"] == "provider_result":
                expected_usage = {"input_tokens": run["input_tokens"], "cached_input_tokens": run["cached_input_tokens"], "output_tokens": run["output_tokens"]}
                usage = payload.get("usage") or {}
                if (payload.get("status") != run["status"] or payload.get("exit_code") != run["exit_code"] or
                    payload.get("output_bytes") != len((run["output"] or "").encode("utf-8")) or
                    any(usage.get(k) != v for k, v in expected_usage.items())):
                    errors.append("provider event conflicts with durable run")
        if len(set(terminal)) > 1: errors.append("conflicting provider terminal events")
        return errors

    def record_review_evidence(self, *args, **kwargs) -> None:
        raise PermissionError("generic review evidence is disabled; use owning event emitters")

    def _emit_policy_violation(self, run_id: str, payload: dict) -> None:
        self._emit_owned(run_id, "policy_violation", payload, "supervisor")

    def record_authority_event(self, *args, **kwargs) -> None:
        raise PermissionError("authority events must be emitted by a dedicated owning boundary")

    def _append_authority_event(self, event_id: str, run_id: str, task_id: str, project_id: str,
                               provider_id: str, job_id: str, kind: str, payload: dict,
                               provenance: str) -> None:
        from .authority import digest
        if not all((event_id, run_id, task_id, project_id, provider_id, job_id, kind, provenance)):
            raise PermissionError("authority event scope is mandatory")
        if not isinstance(payload, dict): raise TypeError("authority event payload must be an object")
        self.db.execute("INSERT INTO authority_events(event_id,run_id,task_id,project_id,provider_id,job_id,kind,payload_json,provenance,payload_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (event_id, run_id, task_id, project_id, provider_id, job_id, kind,
                         json.dumps(payload, sort_keys=True), provenance, digest(payload), self._now()))
        self.db.commit()

    def _emit_owned(self, run_id: str, kind: str, payload: dict, provenance: str) -> None:
        run = self.db.execute("SELECT task_id,provider FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run: raise ValueError("unknown run")
        self._append_authority_event(f"event-{__import__('uuid').uuid4().hex}", run_id, run["task_id"],
                                    self.project_id(), run["provider"], run_id,
                                    kind, payload, provenance)

    def project_id(self) -> str:
        identity = self.identity()
        if not identity or not identity.get("root"):
            identity = {"root": str(self.path.parent.parent.resolve()), "state": str(self.path.resolve())}
            self.record_identity(identity)
        return identity["root"]

    def _emit_policy_resolved(self, run_id, policy, context_digest):
        self._emit_owned(run_id, "policy_resolved", {"policy": policy.snapshot(), "context_digest": context_digest}, "supervisor")
    def _emit_provider_result(self, run_id, status, exit_code, output_bytes, usage):
        self._emit_owned(run_id, "provider_result", {"status": status, "exit_code": exit_code, "output_bytes": output_bytes, "usage": usage}, "provider")
    def _emit_validation_result(self, run_id, validation_id, command, exit_code, output_bytes):
        self._emit_owned(run_id, "validation_result", {"validation_id": validation_id, "run_id": run_id, "command": command, "exit_code": exit_code, "output_bytes": output_bytes}, "supervisor")
    def _emit_lease_acquired(self, run_id, resource, mode, worker_id):
        self._emit_owned(run_id, "lease_acquired", {"resource": resource, "mode": mode, "worker_id": worker_id}, "store")
    def _emit_approval_consumed(self, run_id, payload):
        self._emit_owned(run_id, "approval_consumed", payload, "store")
    def _emit_runtime_granted(self, run_id, payload):
        self._emit_owned(run_id, "runtime_mutation_granted", payload, "runtime_adapter")
    def _emit_runtime_denied(self, run_id, payload):
        self._emit_owned(run_id, "runtime_mutation_denied", payload, "runtime_adapter")
    def _emit_diff_observed(self, run_id, payload): self._emit_owned(run_id, "diff_observed", payload, "supervisor")
    def _emit_integration_preflight(self, run_id, payload): self._emit_owned(run_id, "integration_preflight", payload, "supervisor")
    def _emit_mutation_preflight(self, run_id, payload): self._emit_owned(run_id, "mutation_preflight", payload, "store")

    def authority_events(self, run_id: str, task_id: str | None = None):
        query = "SELECT * FROM authority_events WHERE run_id=?"; args = [run_id]
        if task_id is not None: query += " AND task_id=?"; args.append(task_id)
        return [dict(row) for row in self.db.execute(query, args)]

    def review_evidence_rows(self, run_id: str):
        return [dict(row) for row in self.db.execute("SELECT * FROM review_evidence WHERE run_id=? ORDER BY id", (run_id,))]

    def reject_task(self, task_id: str, reason: str) -> None:
        self.set_task_status(task_id, "REPAIR")
        self.add_message("REVIEW_FINDING", {"reason": reason}, task_id=task_id)

    def pause_task(self, task_id: str, reason: str = "supervisor pause") -> None:
        if self.task(task_id) is None: raise ValueError(f"unknown task: {task_id}")
        self.set_task_status(task_id, "PAUSED")
        self.add_message("INFORMATION", {"action": "pause", "reason": reason}, task_id=task_id)

    def resume_task(self, task_id: str) -> None:
        task = self.task(task_id)
        if task is None: raise ValueError(f"unknown task: {task_id}")
        if task["status"] not in ("PAUSED", "WAITING_RESOURCE", "WAITING_DEPENDENCY", "WAITING_DECISION", "STALE", "REPAIR"):
            raise ValueError(f"task {task_id} is not resumable from {task['status']}")
        self.set_task_status(task_id, "READY" if self.dependencies_ready(task_id) else "WAITING_DEPENDENCY")

    def release_task_leases(self, task_id: str) -> None:
        with self._queue_lock:
            self.db.execute("DELETE FROM leases WHERE task_id=?", (task_id,))
            self.db.commit()

    def retry_task(self, task_id: str, worker_id: str | None = None) -> None:
        task = self.task(task_id)
        if task is None: raise ValueError(f"unknown task: {task_id}")
        if task["status"] not in ("FAILED", "REPAIR", "DISPUTED", "STALE", "WAITING_DECISION"):
            raise ValueError(f"task {task_id} is not retryable from {task['status']}")
        self.release_task_leases(task_id)
        if worker_id:
            self.set_task_worker(task_id, worker_id)
        self.set_task_status(task_id, "READY" if self.dependencies_ready(task_id) else "WAITING_DEPENDENCY")
        self.add_message("INFORMATION", {"action": "retry", "worker_id": worker_id}, task_id=task_id)

    def cancel_task(self, task_id: str, reason: str = "supervisor cancellation") -> None:
        """Cancel queued or active work and release any logical resource leases."""
        task = self.task(task_id)
        if task is None:
            raise ValueError(f"unknown task: {task_id}")
        if task["status"] in ("ACCEPTED", "DONE"):
            raise ValueError(f"task {task_id} is already complete")
        with self._queue_lock:
            if task["status"] != "FAILED":
                self.set_task_status(task_id, "FAILED")
            self.db.execute("UPDATE managed_queue SET status='CANCELLED',finished_at=? WHERE task_id=? AND status IN ('QUEUED','STARTED')", (self._now(), task_id))
            self.db.commit()
        self.release_task_leases(task_id)
        self.add_message("INFORMATION", {"action": "cancel", "reason": reason}, task_id=task_id)

    def report_conflict(self, task_id: str, worker_id: str, payload: dict[str, Any]) -> None:
        self.set_task_status(task_id, "DISPUTED")
        self.add_message("CONFLICT_REPORT", payload, task_id, worker_id)

    def resolve_conflict(self, decision_id: str, task_id: str, decision: str, reason: str) -> None:
        self.add_decision(decision_id, f"conflict:{task_id}", decision, reason, [task_id])
        self.set_task_status(task_id, "REPAIR")
        self.add_message("INFORMATION", {"decision_id": decision_id, "decision": decision, "reason": reason}, task_id=task_id)

    def dependencies_ready(self, task_id: str) -> bool:
        row = self.db.execute("""SELECT COUNT(*) AS n FROM dependencies d JOIN tasks t ON t.id=d.depends_on
                               WHERE d.task_id=? AND t.status NOT IN ('ACCEPTED','DONE')""", (task_id,)).fetchone()
        return row["n"] == 0

    def add_resource(self, name: str, kind: str, paths=()) -> None:
        if not name or not kind:
            raise ValueError("resource name and kind are required")
        if self.db.execute("SELECT 1 FROM resources WHERE name=?", (name,)).fetchone() is not None:
            raise ValueError(f"resource already exists: {name}")
        self.db.execute("INSERT INTO resources(name,kind,paths) VALUES(?,?,?)", (name, kind, json.dumps(list(paths))))
        self.db.commit()

    def consume_resource(self, task_id: str, resource: str) -> int:
        row = self.db.execute("SELECT version FROM resources WHERE name=?", (resource,)).fetchone()
        if row is None:
            raise ValueError(f"unknown resource: {resource}")
        task = self.task(task_id)
        if task is None:
            raise ValueError(f"unknown task: {task_id}")
        versions = json.loads(task["resource_versions"] or "{}")
        versions[resource] = row["version"]
        self.db.execute("UPDATE tasks SET resource_versions=?,updated_at=? WHERE id=?", (json.dumps(versions), self._now(), task_id))
        self.db.commit()
        return row["version"]

    def capture_declared_resources(self, task_id: str) -> dict[str, int]:
        """Snapshot all declared resource versions for a new worker attempt."""
        resources = self.db.execute(
            "SELECT resource FROM task_resources WHERE task_id=? ORDER BY resource", (task_id,)
        ).fetchall()
        return {row["resource"]: self.consume_resource(task_id, row["resource"]) for row in resources}

    def declare_resource_access(self, task_id: str, resource: str, mode: str) -> None:
        if mode not in self.LEASE_MODES:
            raise ValueError(f"invalid resource access mode: {mode}")
        if self.task(task_id) is None: raise ValueError(f"unknown task: {task_id}")
        if self.db.execute("SELECT 1 FROM resources WHERE name=?", (resource,)).fetchone() is None:
            raise ValueError(f"unknown resource: {resource}")
        self.db.execute("INSERT OR REPLACE INTO task_resources(task_id,resource,mode) VALUES(?,?,?)", (task_id, resource, mode))
        self.db.commit()

    def bump_resource(self, resource: str) -> int:
        self.db.execute("UPDATE resources SET version=version+1 WHERE name=?", (resource,))
        row = self.db.execute("SELECT version FROM resources WHERE name=?", (resource,)).fetchone()
        if row is None:
            raise ValueError(f"unknown resource: {resource}")
        new_version = row["version"]
        for task in self.tasks():
            versions = json.loads(task["resource_versions"] or "{}")
            if resource in versions and versions[resource] < new_version and task["status"] not in ("ACCEPTED", "DONE"):
                self.set_task_status(task["id"], "STALE")
                self.add_message("INFORMATION", {
                    "action": "resource_stale", "resource": resource,
                    "previous_version": versions[resource], "current_version": new_version,
                }, task_id=task["id"], worker_id=task["worker_id"])
        self.db.commit()
        return new_version

    def acquire(self, resource: str, task_id: str, worker_id: str, mode: str, ttl: int = 300) -> bool:
        if mode not in self.LEASE_MODES:
            raise ValueError(f"invalid lease mode: {mode}")
        if ttl <= 0:
            raise ValueError("lease ttl must be positive")
        if self.db.execute("SELECT 1 FROM resources WHERE name=?", (resource,)).fetchone() is None:
            raise ValueError(f"unknown resource: {resource}")
        if self.task(task_id) is None:
            raise ValueError(f"unknown task: {task_id}")
        now = self._now()
        # Serialize the check and write across independent Store connections;
        # a deferred transaction would allow two contenders to pass the check
        # before either lease became visible.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("DELETE FROM leases WHERE expires_at<?", (now,))
            existing = self.db.execute("SELECT mode,task_id FROM leases WHERE resource=? AND expires_at>?", (resource, now)).fetchall()
            conflict = any(row["task_id"] != task_id and (row["mode"] == "WRITE" or mode == "WRITE") for row in existing)
            if conflict:
                self.db.commit()
                return False
            self.db.execute("INSERT OR REPLACE INTO leases(resource,task_id,worker_id,mode,acquired_at,expires_at) VALUES(?,?,?,?,?,?)",
                            (resource, task_id, worker_id, mode, now, now + ttl))
            self.db.commit()
            latest = self.latest_run(task_id)
            if latest:
                self._emit_lease_acquired(latest["id"], resource, mode, worker_id)
            return True
        except Exception:
            self.db.rollback()
            raise

    def renew(self, resource: str, task_id: str, worker_id: str, ttl: int = 300) -> bool:
        """Extend a live lease only when it is still owned by this worker."""
        if ttl <= 0:
            raise ValueError("lease ttl must be positive")
        if self.db.execute("SELECT 1 FROM resources WHERE name=?", (resource,)).fetchone() is None:
            raise ValueError(f"unknown resource: {resource}")
        if self.task(task_id) is None:
            raise ValueError(f"unknown task: {task_id}")
        now = self._now()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            updated = self.db.execute(
                "UPDATE leases SET expires_at=? WHERE resource=? AND task_id=? AND worker_id=? AND expires_at>?",
                (now + ttl, resource, task_id, worker_id, now),
            ).rowcount
            self.db.commit()
            return updated == 1
        except Exception:
            self.db.rollback()
            raise

    def add_worker(self, worker_id: str, provider: str, session_id: str | None = None, worktree: str | None = None) -> None:
        now = self._now()
        self.db.execute("""INSERT INTO workers(id,provider,session_id,status,worktree,created_at,updated_at)
                          VALUES(?,?,?,?,?,?,?)
                          ON CONFLICT(id) DO UPDATE SET provider=excluded.provider,
                          session_id=excluded.session_id,status='RUNNING',worktree=excluded.worktree,
                          updated_at=excluded.updated_at""",
                        (worker_id, provider, session_id, "RUNNING", worktree, now, now))
        self.db.commit()

    def set_worker_status(self, worker_id: str, status: str, session_id: str | None = None) -> None:
        with self._queue_lock:
            self.db.execute("UPDATE workers SET status=?,session_id=COALESCE(?,session_id),updated_at=? WHERE id=?",
                            (status, session_id, self._now(), worker_id))
            self.db.commit()

    def set_task_worker(self, task_id: str, worker_id: str, base_commit: str | None = None) -> None:
        self.db.execute("UPDATE tasks SET worker_id=?,base_commit=COALESCE(?,base_commit),updated_at=? WHERE id=?",
                        (worker_id, base_commit, self._now(), task_id))
        self.db.commit()

    def start_run(self, run_id: str, task_id: str, worker_id: str, provider: str, session_id: str | None = None, profile=None,
                  conversation_id: str | None = None, provider_session_id: str | None = None,
                  resume_session_id: str | None = None, context_checkpoint: dict | None = None,
                  requested_budget: dict | None = None, effective_budget: dict | None = None,
                  enforcement_source: str | None = None, budget_mode: str = "soft") -> None:
        profile_json = json.dumps(profile.snapshot() if hasattr(profile, "snapshot") else (profile or {}), sort_keys=True)
        self.db.execute("INSERT INTO runs(id,task_id,worker_id,provider,session_id,provider_session_id,resume_session_id,conversation_id,context_checkpoint,status,profile_json,requested_budget_json,effective_budget_json,enforcement_source,budget_mode,started_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (run_id, task_id, worker_id, provider, session_id, provider_session_id or session_id, resume_session_id, conversation_id,
                         json.dumps(context_checkpoint, sort_keys=True) if context_checkpoint else None, "RUNNING", profile_json,
                         json.dumps(requested_budget or {}, sort_keys=True), json.dumps(effective_budget or {}, sort_keys=True), enforcement_source, budget_mode, self._now()))
        self.db.commit()

    def record_run_authority(self, run_id: str, policy, context=None) -> None:
        from .authority import digest
        snapshot = context.snapshot() if hasattr(context, "snapshot") else context
        self.db.execute("INSERT OR REPLACE INTO run_authority(run_id,mode,capabilities,context_digest,context_json,created_at) VALUES(?,?,?,?,?,?)",
                        (run_id, policy.mode.value, json.dumps(sorted(policy.capabilities)), digest(snapshot) if snapshot else None,
                         json.dumps(snapshot, sort_keys=True) if snapshot else None, self._now()))
        self.db.commit()
        self._emit_policy_resolved(run_id, policy, digest(snapshot) if snapshot else None)

    def record_context_manifest(self, run_id: str, files: list[dict], provenance: str = "explicit-allowlist") -> None:
        total = sum(int(item.get("bytes", 0)) for item in files)
        self.db.execute("INSERT OR REPLACE INTO context_manifests(run_id,files_json,total_bytes,provenance,created_at) VALUES(?,?,?,?,?)",
                        (run_id, json.dumps(files, sort_keys=True), total, provenance, self._now())); self.db.commit()

    def refresh_run_authority(self, run_id: str, policy, context) -> None:
        """Supervisor-only durable refresh after a material authority/state change."""
        if not self.db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
            raise ValueError("unknown run")
        self.record_run_authority(run_id, policy, context)
        self._emit_policy_resolved(run_id, policy, context.digest() if hasattr(context, "digest") else None)

    def authority_snapshot(self, run_id: str):
        row = self.db.execute("SELECT * FROM run_authority WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def create_approval(self, token) -> None:
        if not all((token.project_id, token.provider_id, token.job_id, token.idempotency_key, token.run_id, token.task_id)):
            raise PermissionError("approval scope must include run, task, project, provider, job, and idempotency key")
        self.db.execute("INSERT INTO approvals(token_id,capability,project_id,provider_id,job_id,payload_hash,idempotency_key,max_attempts,attempts,expires_at,invalidated,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (token.token_id, token.capability, token.project_id, token.provider_id, token.job_id,
                         token.payload_hash, token.idempotency_key, token.max_attempts, token.attempts,
                         token.expires_at, int(token.invalidated), self._now()))
        self.db.execute("UPDATE approvals SET run_id=?,task_id=? WHERE token_id=?", (token.run_id, token.task_id, token.token_id))
        self.db.commit()

    def consume_approval(self, token_id: str, policy, payload, project_id=None, provider_id=None,
                         job_id=None, idempotency_key=None, run_id=None, task_id=None):
        from .authority import ApprovalToken
        if not all((project_id, provider_id, job_id, idempotency_key, run_id, task_id)):
            raise PermissionError("approval scope is mandatory")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT * FROM approvals WHERE token_id=?", (token_id,)).fetchone()
            if not row:
                raise PermissionError("approval token not found")
            values = {key: row[key] for key in ("token_id", "capability", "project_id", "provider_id", "job_id",
                                             "payload_hash", "idempotency_key", "max_attempts", "attempts", "expires_at", "run_id", "task_id")}
            values["invalidated"] = bool(row["invalidated"])
            token = ApprovalToken(**values)
            if not token.usable(policy, payload, project_id=project_id, provider_id=provider_id,
                                job_id=job_id, idempotency_key=idempotency_key,
                                run_id=run_id, task_id=task_id):
                raise PermissionError("approval token is invalid, expired, exhausted, or payload-bound differently")
            updated = self.db.execute("UPDATE approvals SET attempts=attempts+1 WHERE token_id=? AND attempts<? AND invalidated=0",
                                      (token_id, token.max_attempts)).rowcount
            if updated != 1: raise PermissionError("approval token was consumed concurrently")
            self.db.commit()
            if token.run_id and self.db.execute("SELECT 1 FROM runs WHERE id=?", (token.run_id,)).fetchone():
                self._emit_approval_consumed(token.run_id, {"token_id": token_id, "capability": token.capability,
                                                                          "project_id": token.project_id, "provider_id": token.provider_id,
                                                                          "job_id": token.job_id, "run_id": token.run_id,
                                                                          "task_id": token.task_id, "idempotency_key": token.idempotency_key,
                                                                          "payload_hash": token.payload_hash, "attempt": token.attempts + 1})
            return token
        except Exception:
            self.db.rollback()
            raise

    def invalidate_approval(self, token_id: str) -> None:
        self.db.execute("UPDATE approvals SET invalidated=1 WHERE token_id=?", (token_id,))
        self.db.commit()

    def record_identity(self, identity: dict) -> None:
        base = dict(identity)
        base.pop("dirty", None)
        base.pop("update_channel", None)
        encoded = json.dumps(base, sort_keys=True)
        existing = self.db.execute("SELECT identity_json FROM installation_identity WHERE id=1").fetchone()
        if existing:
            if existing[0] != encoded: raise PermissionError("installation identity cannot be replaced in place")
            return
        try:
            self.db.execute("INSERT INTO installation_identity(id,identity_json,updated_at) VALUES(1,?,?)", (encoded, self._now()))
            self.db.commit()
        except sqlite3.IntegrityError:
            self.db.rollback()
            current = self.db.execute("SELECT identity_json FROM installation_identity WHERE id=1").fetchone()
            if not current or current[0] != encoded: raise PermissionError("installation identity cannot be replaced in place")

    def record_identity_observation(self, identity: dict) -> None:
        self.db.execute("INSERT INTO identity_observations(identity_json,observed_at) VALUES(?,?)",
                        (json.dumps(identity, sort_keys=True), self._now()))
        self.db.commit()

    def identity(self):
        row = self.db.execute("SELECT identity_json FROM installation_identity WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def record_blocker(self, task_id: str, blocker_key: str, external_state_version: str, threshold: int = 3) -> bool:
        row = self.db.execute("SELECT external_state_version,count FROM blocker_events WHERE task_id=? AND blocker_key=?", (task_id, blocker_key)).fetchone()
        count = row["count"] + 1 if row and row["external_state_version"] == external_state_version else 1
        self.db.execute("INSERT OR REPLACE INTO blocker_events(task_id,blocker_key,external_state_version,count,updated_at) VALUES(?,?,?,?,?)",
                        (task_id, blocker_key, external_state_version, count, self._now()))
        self.db.commit(); return count >= threshold

    def finish_run(self, run_id: str, status: str, exit_code: int, output: str, session_id: str | None = None,
                   failure_class: str | None = None, usage: dict | None = None) -> None:
        # A single Store connection is shared by managed waiters; serialize the
        # complete read/merge/update/emit transaction, not only commit().
        with self._queue_lock:
            self._finish_run_unlocked(run_id, status, exit_code, output, session_id, failure_class, usage)

    def _finish_run_unlocked(self, run_id: str, status: str, exit_code: int, output: str, session_id: str | None = None,
                   failure_class: str | None = None, usage: dict | None = None) -> None:
        meta = usage or {}
        row = self.db.execute("SELECT effective_budget_json,budget_mode FROM runs WHERE id=?", (run_id,)).fetchone()
        effective = json.loads(row[0] or "{}") if row else {}
        effective.update({key: meta[key] for key in ("prompt_bytes", "prompt_context_bytes", "input_tokens", "cached_input_tokens", "output_tokens", "tool_output_bytes", "files_read", "file_bytes", "commands_executed", "model_turns", "wall_time_ms") if key in meta})
        self.db.execute("UPDATE runs SET status=?,exit_code=?,output=?,session_id=COALESCE(?,session_id),failure_class=?,effective_budget_json=?,budget_mode=?,input_tokens=?,cached_input_tokens=?,output_tokens=?,output_omitted_bytes=?,output_provenance=?,tool_output_bytes=?,files_read=?,file_bytes=?,prompt_bytes=?,model_turns=?,wall_time_ms=?,commands_executed=?,soft_budget_exceeded=?,finished_at=? WHERE id=?",
                        (status, exit_code, output, session_id, failure_class,
                         json.dumps(effective, sort_keys=True), "hard" if failure_class == "hard_budget_denied" else ("soft" if meta.get("soft_budget_exceeded") or (row and row["budget_mode"] == "soft") else "observed"), (usage or {}).get("input_tokens"),
                         (usage or {}).get("cached_input_tokens"), (usage or {}).get("output_tokens"), meta.get("output_omitted_bytes", 0), meta.get("output_provenance"), meta.get("tool_output_bytes", 0), meta.get("files_read", 0), meta.get("file_bytes", 0), meta.get("prompt_bytes", 0), meta.get("model_turns", 0), meta.get("wall_time_ms", 0), meta.get("commands_executed", 0), int(meta.get("soft_budget_exceeded", False)), self._now(), run_id))
        self.db.commit()
        self._emit_provider_result(run_id, status, exit_code, len(output.encode("utf-8")), meta)

    def set_run_phase(self, run_id: str, phase: str) -> None:
        allowed = {"QUEUED", "STARTING", "PROVIDER_RUNNING", "DISCONNECTED", "ORPHANED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"}
        if phase not in allowed: raise ValueError("invalid run phase")
        self.db.execute("UPDATE runs SET status=? WHERE id=?", (phase, run_id)); self.db.commit()

    def latest_run(self, task_id: str, worker_id: str | None = None):
        if worker_id is None:
            return self.db.execute("SELECT * FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1", (task_id,)).fetchone()
        return self.db.execute("SELECT * FROM runs WHERE task_id=? AND worker_id=? ORDER BY started_at DESC LIMIT 1", (task_id, worker_id)).fetchone()

    def heartbeat(self, run_id: str, action: str | None = None, ttl: float = 30.0) -> None:
        with self._queue_lock:
            if not self.db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone(): raise ValueError("unknown run")
            if ttl <= 0: raise ValueError("heartbeat ttl must be positive")
            now = self._now()
            self.db.execute("UPDATE runs SET heartbeat_at=?,heartbeat_deadline=?,action_count=action_count+? WHERE id=?", (now, now + ttl, int(action is not None), run_id))
            self.db.commit()

    def bind_process_identity(self, run_id: str, host_instance_id: str, pid: int, process_start_identity: str, heartbeat_deadline: float) -> None:
        self.db.execute("UPDATE runs SET host_instance_id=?,owner_pid=?,process_start_identity=?,heartbeat_deadline=? WHERE id=?",
                        (host_instance_id, pid, process_start_identity, heartbeat_deadline, run_id)); self.db.commit()

    def activity_snapshot(self, run_id: str) -> dict[str, Any]:
        run = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run: raise ValueError("unknown run")
        return {"run": dict(run), "authority": self.authority_snapshot(run_id),
                "events": self.authority_events(run_id),
                "validations": [dict(row) for row in self.db.execute("SELECT * FROM validations WHERE task_id=? ORDER BY id", (run["task_id"],))],
                "leases": [dict(row) for row in self.db.execute("SELECT * FROM leases WHERE task_id=?", (run["task_id"],))],
                "messages": [dict(row) for row in self.db.execute("SELECT * FROM messages WHERE task_id=? ORDER BY id", (run["task_id"],))]}

    def result_only_snapshot(self, task_id: str, byte_limit: int = 8192) -> dict[str, Any]:
        """Return one compact task outcome without historical or transcript data."""
        if byte_limit < 512 or byte_limit > 16384:
            raise ValueError("result_only byte_limit must be 512..16384")
        task = self.task(task_id)
        if task is None:
            raise ValueError(f"unknown task: {task_id}")
        run = self.latest_run(task_id)
        message = self.db.execute(
            "SELECT id,type,payload,created_at FROM messages WHERE task_id=? AND type IN ('QUESTION','TASK_COMPLETE','BLOCKER') ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        payload = {}
        if message:
            try: payload = json.loads(message["payload"])
            except json.JSONDecodeError: payload = {"summary": "invalid stored message payload"}
        # Large provider output belongs in durable evidence, never status.
        for key in ("output", "stdout", "activity", "transcript", "state"):
            payload.pop(key, None)
        result = {
            "task": {"id": task["id"], "title": task["title"], "status": task["status"], "worker_id": task["worker_id"]},
            "latest": ({"event_id": message["id"], "type": message["type"], "payload": payload} if message else None),
            "run": ({"id": run["id"], "status": run["status"], "exit_code": run["exit_code"], "failure_class": run["failure_class"]} if run else None),
            "inspection_mode": "result_only",
        }
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > byte_limit:
            result["latest"] = {"event_id": message["id"], "type": message["type"], "payload": {"summary": "result omitted; inspect evidence by id"}} if message else None
            encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > byte_limit:
            raise ValueError("result_only metadata exceeds configured byte limit")
        result["payload_bytes"] = len(encoded)
        result["payload_limit"] = byte_limit
        return result

    def queue_steering(self, run_id: str, instruction: str, scope: str) -> None:
        if not instruction or not scope: raise ValueError("scoped queued steering requires instruction and scope")
        row = self.db.execute("SELECT steering_json FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row: raise ValueError("unknown run")
        items = json.loads(row[0]); items.append({"instruction": instruction, "scope": scope, "created_at": self._now()})
        self.db.execute("UPDATE runs SET steering_json=? WHERE id=?", (json.dumps(items, sort_keys=True), run_id)); self.db.commit()

    def enqueue_managed(self, task_id: str, worker_id: str, provider: str, reason: str = "capacity", payload: dict | None = None) -> int:
        with self._queue_lock:
            cur = self.db.execute("INSERT INTO managed_queue(task_id,worker_id,provider,reason,queued_at,payload_json) VALUES(?,?,?,?,?,?)",
                                  (task_id, worker_id, provider, reason, self._now(), json.dumps(payload or {}, sort_keys=True, default=str)))
            self.db.commit(); return int(cur.lastrowid)

    def admit_managed(self, task_id: str, worker_id: str, provider: str, capacity: int) -> tuple[int, str]:
        """Atomically reserve a durable slot before launching a provider."""
        if capacity < 1: raise ValueError("managed capacity must be positive")
        with self._queue_lock:
            self.db.commit()
            self.db.execute("BEGIN IMMEDIATE")
            active = self.db.execute("SELECT COUNT(*) FROM managed_queue WHERE status='STARTED'").fetchone()[0]
            reason = "global capacity" if active >= capacity else "scheduled"
            cur = self.db.execute("INSERT INTO managed_queue(task_id,worker_id,provider,reason,status,queued_at,started_at) VALUES(?,?,?,?,?,?,?)",
                                  (task_id, worker_id, provider, reason, "QUEUED" if reason != "scheduled" else "STARTED", self._now(), self._now() if reason == "scheduled" else None))
            self.db.commit()
            return int(cur.lastrowid), reason

    def managed_queue(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM managed_queue ORDER BY id")]

    def mark_managed_started(self, queue_id: int) -> None:
        with self._queue_lock:
            self.db.execute("UPDATE managed_queue SET status='STARTED',started_at=? WHERE id=? AND status='QUEUED'", (self._now(), queue_id)); self.db.commit()

    def set_managed_payload(self, queue_id: int, payload: dict) -> None:
        with self._queue_lock:
            self.db.execute("UPDATE managed_queue SET payload_json=? WHERE id=? AND status='QUEUED'",
                            (json.dumps(payload, sort_keys=True, default=str), queue_id)); self.db.commit()

    def mark_managed_finished(self, queue_id: int, status: str = "COMPLETED") -> None:
        with self._queue_lock:
            self.db.execute("UPDATE managed_queue SET status=?,finished_at=? WHERE id=?", (status, self._now(), queue_id)); self.db.commit()

    def reconcile(self, host_instance_id: str | None = None) -> list[str]:
        """Recover externally-dead managed processes without trusting stale locks."""
        with self._reconcile_lock:
            # Serialize the complete state transition across independent Store
            # connections; recovery must not expose half-updated leases/tasks.
            self.db.execute("BEGIN IMMEDIATE")
            recovered = []
            for worker in self.db.execute("SELECT * FROM workers WHERE status='RUNNING'").fetchall():
                session = worker["session_id"] or ""
                active_run = self.db.execute("SELECT * FROM runs WHERE worker_id=? AND status IN ('RUNNING','STARTING','PROVIDER_RUNNING','DISCONNECTED') ORDER BY started_at DESC LIMIT 1", (worker["id"],)).fetchone()
                # Provider-native thread IDs do not expose a local PID. They
                # still must fail closed on host loss or heartbeat expiry.
                if active_run and host_instance_id and active_run["host_instance_id"] and active_run["host_instance_id"] != host_instance_id:
                    self.db.execute("UPDATE runs SET status='ORPHANED' WHERE id=?", (active_run["id"],))
                    self.db.execute("UPDATE workers SET status='ORPHANED',updated_at=? WHERE id=?", (self._now(), worker["id"]))
                    task_row = self.db.execute("SELECT id FROM tasks WHERE worker_id=? AND status='RUNNING'", (worker["id"],)).fetchone()
                    if task_row:
                        self.db.execute("UPDATE tasks SET status='WAITING_DECISION',updated_at=? WHERE id=?", (self._now(), task_row["id"]))
                        self.db.execute("DELETE FROM leases WHERE task_id=?", (task_row["id"],))
                        self.db.execute("INSERT INTO messages(type,task_id,worker_id,payload,created_at) VALUES(?,?,?,?,?)", ("BLOCKER", task_row["id"], worker["id"], json.dumps({"reason": "previous MCP host no longer owns run", "recovery": "resume provider session or restart task"}), self._now()))
                    continue
                if active_run and active_run["heartbeat_deadline"] and active_run["heartbeat_deadline"] < self._now():
                    self.db.execute("UPDATE runs SET status='DISCONNECTED' WHERE id=?", (active_run["id"],))
                    self.db.execute("UPDATE workers SET status='DISCONNECTED',updated_at=? WHERE id=?", (self._now(), worker["id"]))
                    task_row = self.db.execute("SELECT id FROM tasks WHERE worker_id=? AND status='RUNNING'", (worker["id"],)).fetchone()
                    if task_row:
                        self.db.execute("UPDATE tasks SET status='WAITING_DECISION',updated_at=? WHERE id=?", (self._now(), task_row["id"]))
                        self.db.execute("DELETE FROM leases WHERE task_id=?", (task_row["id"],))
                        self.db.execute("INSERT INTO messages(type,task_id,worker_id,payload,created_at) VALUES(?,?,?,?,?)", ("BLOCKER", task_row["id"], worker["id"], json.dumps({"reason": "heartbeat expired", "recovery": "reconcile or resume provider session"}), self._now()))
                    continue
                if not session.startswith("pid:"):
                    continue
                try:
                    os.kill(int(session[4:]), 0); alive = True
                except (ValueError, ProcessLookupError, PermissionError):
                    alive = False
                if alive:
                    run = self.db.execute("SELECT * FROM runs WHERE worker_id=? AND status IN ('RUNNING','STARTING','PROVIDER_RUNNING','DISCONNECTED') ORDER BY started_at DESC LIMIT 1", (worker["id"],)).fetchone()
                    if run and host_instance_id and run["host_instance_id"] and run["host_instance_id"] != host_instance_id:
                        self.db.execute("UPDATE runs SET status='ORPHANED' WHERE id=?", (run["id"],))
                        self.db.execute("UPDATE workers SET status='ORPHANED',updated_at=? WHERE id=?", (self._now(), worker["id"]))
                        task_row = self.db.execute("SELECT id,status FROM tasks WHERE worker_id=? AND status='RUNNING'", (worker["id"],)).fetchone()
                        if task_row:
                            self.db.execute("UPDATE tasks SET status='WAITING_DECISION',updated_at=? WHERE id=?", (self._now(), task_row["id"]))
                            self.db.execute("DELETE FROM leases WHERE task_id=?", (task_row["id"],))
                            self.db.execute("INSERT INTO messages(type,task_id,worker_id,payload,created_at) VALUES(?,?,?,?,?)", ("BLOCKER", task_row["id"], worker["id"], json.dumps({"reason": "previous MCP host no longer owns run", "recovery": "resume provider session or restart task"}), self._now()))
                        continue
                    if run and run["process_start_identity"] and process_start_identity(int(session[4:])) != run["process_start_identity"]:
                        alive = False
                    else:
                        if run and run["heartbeat_deadline"] and run["heartbeat_deadline"] < self._now():
                            self.db.execute("UPDATE runs SET status='DISCONNECTED' WHERE id=?", (run["id"],))
                            self.db.execute("UPDATE workers SET status='DISCONNECTED',updated_at=? WHERE id=?", (self._now(), worker["id"]))
                            task_row = self.db.execute("SELECT id,status FROM tasks WHERE worker_id=? AND status='RUNNING'", (worker["id"],)).fetchone()
                            if task_row:
                                self.db.execute("UPDATE tasks SET status='WAITING_DECISION',updated_at=? WHERE id=?", (self._now(), task_row["id"]))
                                self.db.execute("DELETE FROM leases WHERE task_id=?", (task_row["id"],))
                                self.db.execute("INSERT INTO messages(type,task_id,worker_id,payload,created_at) VALUES(?,?,?,?,?)", ("BLOCKER", task_row["id"], worker["id"], json.dumps({"reason": "heartbeat expired", "recovery": "reconcile or resume provider session"}), self._now()))
                        continue
                worker_id = worker["id"]
                self.db.execute("UPDATE workers SET status='FAILED',updated_at=? WHERE id=?", (self._now(), worker_id))
                self.db.execute("UPDATE runs SET status='FAILED',exit_code=137,output='process disappeared',finished_at=? WHERE worker_id=? AND status IN ('RUNNING','STARTING','PROVIDER_RUNNING','DISCONNECTED')", (self._now(), worker_id))
                task_rows = self.db.execute("SELECT id FROM tasks WHERE worker_id=? AND status='RUNNING'", (worker_id,)).fetchall()
                for task in task_rows:
                    self.db.execute("UPDATE tasks SET status='FAILED',updated_at=? WHERE id=?", (self._now(), task["id"]))
                    self.db.execute("DELETE FROM leases WHERE task_id=?", (task["id"],)); recovered.append(task["id"])
            # A restarted MCP host loses process-local handles. Durable queue
            # rows must not remain STARTED forever when no live run owns them.
            for queued in self.db.execute("SELECT * FROM managed_queue WHERE status='STARTED'").fetchall():
                run = self.db.execute("SELECT status FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1", (queued["task_id"],)).fetchone()
                if not run or run["status"] not in {"RUNNING", "STARTING", "PROVIDER_RUNNING"}:
                    self.db.execute("UPDATE managed_queue SET status='ORPHANED',finished_at=? WHERE id=?", (self._now(), queued["id"]))
            self.db.commit()
            return recovered

    def add_message(self, msg_type: str, payload: dict[str, Any], task_id=None, worker_id=None) -> None:
        with self._queue_lock:
            self.db.execute("INSERT INTO messages(type,task_id,worker_id,payload,created_at) VALUES(?,?,?,?,?)",
                            (msg_type, task_id, worker_id, json.dumps(payload), self._now()))
            self.db.commit()

    def inbox(self):
        return self.db.execute("SELECT * FROM messages WHERE acknowledged=0 ORDER BY created_at").fetchall()

    def acknowledge_messages(self, ids=()) -> None:
        with self._queue_lock:
            ids = list(ids)
            if ids:
                self.db.executemany("UPDATE messages SET acknowledged=1 WHERE id=?", ((x,) for x in ids))
            else:
                self.db.execute("UPDATE messages SET acknowledged=1 WHERE acknowledged=0")
            self.db.commit()

    def add_decision(self, decision_id: str, topic: str, decision: str, reason: str, affected_tasks=()) -> None:
        self.db.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?)",
                        (decision_id, "accepted", topic, decision, reason, json.dumps(list(affected_tasks)), self._now()))
        self.db.commit()

    def add_validation(self, task_id: str, command: str, exit_code: int, output: str) -> None:
        cursor = self.db.execute("INSERT INTO validations(task_id,command,exit_code,output,created_at) VALUES(?,?,?,?,?)",
                        (task_id, command, exit_code, output, self._now()))
        self.db.commit()
        latest = self.latest_run(task_id)
        if latest:
            self._emit_validation_result(latest["id"], cursor.lastrowid, command, exit_code, len(output.encode("utf-8")))

    def record_integration(self, task_id: str, commit: str, strategy: str = "cherry-pick") -> None:
        self.db.execute("INSERT INTO integrations(task_id,commit_hash,strategy,status,created_at) VALUES(?,?,?,?,?)",
                        (task_id, commit, strategy, "ACCEPTED", self._now()))
        self.db.commit()

    def snapshot(self) -> dict[str, Any]:
        return {"tasks": [dict(x) for x in self.tasks()], "workers": [dict(x) for x in self.db.execute("SELECT * FROM workers")],
                "runs": [dict(x) for x in self.db.execute("SELECT * FROM runs ORDER BY started_at")],
                "dependencies": [dict(x) for x in self.db.execute("SELECT * FROM dependencies ORDER BY task_id,depends_on")],
                "resources": [dict(x) for x in self.db.execute("SELECT * FROM resources ORDER BY name")],
                "task_resources": [dict(x) for x in self.db.execute("SELECT * FROM task_resources ORDER BY task_id,resource")],
                "integrations": [dict(x) for x in self.db.execute("SELECT * FROM integrations ORDER BY created_at")],
                "leases": [dict(x) for x in self.db.execute("SELECT * FROM leases WHERE expires_at>?", (self._now(),))],
                "unread_messages": len(self.inbox()), "decisions": [dict(x) for x in self.db.execute("SELECT * FROM decisions ORDER BY created_at")],
                "validations": [dict(x) for x in self.db.execute("SELECT * FROM validations ORDER BY created_at")]} 

    def dashboard_snapshot(self, recent_limit: int = 12) -> dict[str, Any]:
        """Fast UI summary that never loads run output or validation stdout."""
        recent_limit = max(1, min(50, int(recent_limit)))
        task_counts = {row["status"]: row["count"] for row in self.db.execute(
            "SELECT status,COUNT(*) AS count FROM tasks GROUP BY status")}
        worker_counts = {row["status"]: row["count"] for row in self.db.execute(
            "SELECT status,COUNT(*) AS count FROM workers GROUP BY status")}
        recent = [dict(row) for row in self.db.execute(
            "SELECT id,title,status,worker_id,updated_at FROM tasks ORDER BY updated_at DESC LIMIT ?", (recent_limit,))]
        return {"task_counts": task_counts, "worker_counts": worker_counts,
                "recent_tasks": recent, "unread_messages": len(self.inbox())}
