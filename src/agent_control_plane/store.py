from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import time
from pathlib import Path
from typing import Any


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
  provider TEXT, worker_id TEXT, base_commit TEXT, resource_versions TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL, updated_at REAL NOT NULL
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
  status TEXT NOT NULL, exit_code INTEGER, output TEXT NOT NULL DEFAULT '',
  started_at REAL NOT NULL, finished_at REAL
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
CREATE TABLE IF NOT EXISTS control_settings (
  id INTEGER PRIMARY KEY CHECK(id=1), policy TEXT NOT NULL DEFAULT 'shared_queue', updated_at REAL NOT NULL
);
"""


class Store:
    TASK_STATUSES = {"READY", "RUNNING", "REVIEW", "ACCEPTED", "DONE", "FAILED", "PAUSED",
                     "WAITING_RESOURCE", "WAITING_DEPENDENCY", "WAITING_DECISION", "STALE", "DISPUTED", "REPAIR"}
    LEASE_MODES = {"READ", "WRITE"}
    TASK_TRANSITIONS = {
        "READY": {"RUNNING", "WAITING_RESOURCE", "WAITING_DEPENDENCY", "WAITING_DECISION", "PAUSED", "FAILED", "DISPUTED", "STALE"},
        "RUNNING": {"REVIEW", "FAILED", "PAUSED", "DISPUTED", "STALE"},
        "REVIEW": {"ACCEPTED", "REPAIR", "DISPUTED", "STALE"},
        "ACCEPTED": {"DONE"}, "DONE": set(), "FAILED": {"READY", "REPAIR", "STALE"},
        "PAUSED": {"READY", "RUNNING", "FAILED", "STALE"}, "WAITING_RESOURCE": {"READY", "RUNNING", "PAUSED", "STALE"},
        "WAITING_DEPENDENCY": {"READY", "RUNNING", "PAUSED", "STALE"}, "WAITING_DECISION": {"READY", "REPAIR", "PAUSED", "STALE"},
        "STALE": {"READY", "REPAIR", "DISPUTED", "PAUSED"}, "DISPUTED": {"REPAIR", "WAITING_DECISION", "PAUSED", "STALE"},
        "REPAIR": {"READY", "RUNNING", "PAUSED", "FAILED", "STALE"},
    }

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Scheduler workers may complete on different threads; SQLite serializes
        # short transactions while WAL improves reader/writer coexistence.
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
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
        configured = json.loads(config_path.read_text()).get("workers", []) if config_path.exists() else []
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

    def knowledge_context(self) -> str:
        row = self.db.execute("SELECT path,digest FROM knowledge ORDER BY id DESC LIMIT 1").fetchone()
        if row is None: return ""
        source = Path(row["path"])
        if not source.is_file(): return ""
        return f"KNOWLEDGE DIGEST: {row['digest']}\nKNOWLEDGE CONTENT:\n{source.read_text()}"

    def _now(self) -> float:
        return time.time()

    def add_task(self, task_id: str, title: str, provider: str | None = None, depends_on=()) -> None:
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
        self.db.execute("INSERT INTO tasks(id,title,status,provider,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                        (task_id, title, "READY", provider, now, now))
        for dep in depends_on:
            self.db.execute("INSERT INTO dependencies(task_id,depends_on) VALUES(?,?)", (task_id, dep))
        self.db.commit()

    def task(self, task_id: str):
        return self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    def tasks(self):
        return self.db.execute("SELECT * FROM tasks ORDER BY created_at").fetchall()

    def set_task_status(self, task_id: str, status: str) -> None:
        if status not in self.TASK_STATUSES:
            raise ValueError(f"invalid task status: {status}")
        current = self.task(task_id)
        if current is None:
            raise ValueError(f"unknown task: {task_id}")
        if status != current["status"] and status not in self.TASK_TRANSITIONS[current["status"]]:
            raise ValueError(f"invalid task transition: {current['status']} -> {status}")
        self.db.execute("UPDATE tasks SET status=?,updated_at=? WHERE id=?", (status, self._now(), task_id))
        self.db.commit()

    def claim_task(self, task_id: str, worker_id: str, base_commit: str | None = None) -> None:
        """Atomically claim a runnable task so duplicate dispatch cannot execute it."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            task = self.task(task_id)
            if task is None:
                raise ValueError(f"unknown task: {task_id}")
            if task["status"] not in ("READY", "REPAIR"):
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
        self.set_task_status(task_id, "ACCEPTED")

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
        if task["status"] not in ("PAUSED", "WAITING_RESOURCE", "WAITING_DEPENDENCY", "STALE", "REPAIR"):
            raise ValueError(f"task {task_id} is not resumable from {task['status']}")
        self.set_task_status(task_id, "READY" if self.dependencies_ready(task_id) else "WAITING_DEPENDENCY")

    def release_task_leases(self, task_id: str) -> None:
        self.db.execute("DELETE FROM leases WHERE task_id=?", (task_id,))
        self.db.commit()

    def retry_task(self, task_id: str, worker_id: str | None = None) -> None:
        task = self.task(task_id)
        if task is None: raise ValueError(f"unknown task: {task_id}")
        if task["status"] not in ("FAILED", "REPAIR", "DISPUTED", "STALE"):
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
        if task["status"] != "FAILED":
            self.set_task_status(task_id, "FAILED")
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
        self.db.execute("UPDATE workers SET status=?,session_id=COALESCE(?,session_id),updated_at=? WHERE id=?",
                        (status, session_id, self._now(), worker_id))
        self.db.commit()

    def set_task_worker(self, task_id: str, worker_id: str, base_commit: str | None = None) -> None:
        self.db.execute("UPDATE tasks SET worker_id=?,base_commit=COALESCE(?,base_commit),updated_at=? WHERE id=?",
                        (worker_id, base_commit, self._now(), task_id))
        self.db.commit()

    def start_run(self, run_id: str, task_id: str, worker_id: str, provider: str, session_id: str | None = None) -> None:
        self.db.execute("INSERT INTO runs(id,task_id,worker_id,provider,session_id,status,started_at) VALUES(?,?,?,?,?,?,?)",
                        (run_id, task_id, worker_id, provider, session_id, "RUNNING", self._now()))
        self.db.commit()

    def finish_run(self, run_id: str, status: str, exit_code: int, output: str, session_id: str | None = None) -> None:
        self.db.execute("UPDATE runs SET status=?,exit_code=?,output=?,session_id=COALESCE(?,session_id),finished_at=? WHERE id=?",
                        (status, exit_code, output, session_id, self._now(), run_id))
        self.db.commit()

    def reconcile(self) -> list[str]:
        """Recover externally-dead managed processes without trusting stale locks."""
        recovered = []
        for worker in self.db.execute("SELECT * FROM workers WHERE status='RUNNING'").fetchall():
            session = worker["session_id"] or ""
            if not session.startswith("pid:"):
                continue
            try:
                os.kill(int(session[4:]), 0)
                alive = True
            except (ValueError, ProcessLookupError, PermissionError):
                alive = False
            if alive:
                continue
            worker_id = worker["id"]
            self.set_worker_status(worker_id, "FAILED")
            self.db.execute("UPDATE runs SET status='FAILED',exit_code=137,output='process disappeared',finished_at=? WHERE worker_id=? AND status='RUNNING'",
                            (self._now(), worker_id))
            task_rows = self.db.execute("SELECT id FROM tasks WHERE worker_id=? AND status='RUNNING'", (worker_id,)).fetchall()
            for task in task_rows:
                self.set_task_status(task["id"], "FAILED")
                self.release_task_leases(task["id"])
                recovered.append(task["id"])
        self.db.commit()
        return recovered

    def add_message(self, msg_type: str, payload: dict[str, Any], task_id=None, worker_id=None) -> None:
        self.db.execute("INSERT INTO messages(type,task_id,worker_id,payload,created_at) VALUES(?,?,?,?,?)",
                        (msg_type, task_id, worker_id, json.dumps(payload), self._now()))
        self.db.commit()

    def inbox(self):
        return self.db.execute("SELECT * FROM messages WHERE acknowledged=0 ORDER BY created_at").fetchall()

    def acknowledge_messages(self, ids=()) -> None:
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
        self.db.execute("INSERT INTO validations(task_id,command,exit_code,output,created_at) VALUES(?,?,?,?,?)",
                        (task_id, command, exit_code, output, self._now()))
        self.db.commit()

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
