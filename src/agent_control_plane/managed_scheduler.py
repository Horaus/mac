"""Small durable managed-run scheduler used by MCP/CLI integrations."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from dataclasses import asdict
from pathlib import Path
from .providers import ProviderAdapter, WorkerResult
from .service import run_worker, start_managed_worker
from .store import Store


@dataclass(frozen=True)
class ManagedJob:
    task_id: str
    worker_id: str
    adapter: ProviderAdapter
    prompt: str
    cwd: str | Path
    resources: tuple[tuple[str, str], ...] = ()
    profile: object = None
    authority: object = None
    context: object = None
    conversation_id: str | None = None
    resume_session_id: str | None = None
    resume_from_task_id: str | None = None
    context_checkpoint: dict | None = None
    preserve_resume_snapshot: bool = False
    lifecycle: str = "batch"


class ManagedScheduler:
    def __init__(self, store: Store, max_workers: int = 2):
        self.store, self.max_workers = store, max_workers
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="mac-managed")
        self._active = 0
        self._pending: list[ManagedJob] = []
        self._lock = __import__("threading").RLock()

    @staticmethod
    def _payload(job: ManagedJob) -> dict:
        return {"task_id": job.task_id, "worker_id": job.worker_id, "prompt": job.prompt,
                "cwd": str(job.cwd), "resources": list(job.resources),
                "profile": job.profile.snapshot() if hasattr(job.profile, "snapshot") else {},
                "authority": job.authority.snapshot() if hasattr(job.authority, "snapshot") else {},
                "context": asdict(job.context) if job.context is not None else None,
                "lifecycle": job.lifecycle,
                "conversation_id": job.conversation_id, "resume_session_id": job.resume_session_id,
                "resume_from_task_id": job.resume_from_task_id, "context_checkpoint": job.context_checkpoint}

    def recover_queued(self, adapter_factory, profile_factory, authority_factory, context_factory, live_callback=None) -> int:
        """Re-submit queued jobs from SQLite after an MCP process restart."""
        recovered = 0
        for row in self.store.managed_queue():
            if row["status"] != "QUEUED" or not row.get("payload_json"):
                continue
            payload = __import__("json").loads(row["payload_json"] or "{}")
            if not payload.get("prompt") or not payload.get("cwd"):
                continue
            job = ManagedJob(row["task_id"], row["worker_id"], adapter_factory(row["provider"]),
                             payload["prompt"], payload["cwd"], tuple(tuple(x) for x in payload.get("resources", [])),
                             profile_factory(payload.get("profile")), authority_factory(payload.get("authority")),
                             context_factory(payload.get("context")), payload.get("conversation_id"),
                             payload.get("resume_session_id"), payload.get("resume_from_task_id"),
                             payload.get("context_checkpoint"), False, payload.get("lifecycle", "batch"))
            if job.lifecycle == "live" and live_callback is not None:
                live_callback(row["id"], job)
            else:
                result = self.submit(job)
                # The old durable row is an attempted dispatch, not the new
                # queue entry created when admission is still unavailable.
                self.store.mark_managed_finished(row["id"], "RECOVERED" if result["status"] == "STARTED" else "REQUEUED")
            recovered += 1
        return recovered

    def submit(self, job: ManagedJob):
        for resource, mode in job.resources:
            if not self.store.acquire(resource, job.task_id, job.worker_id, mode):
                queue_id = self.store.enqueue_managed(job.task_id, job.worker_id, job.adapter.name, f"lease unavailable: {resource}", self._payload(job))
                self.store.set_task_status(job.task_id, "WAITING_RESOURCE")
                return {"status": "QUEUED", "queue_id": queue_id, "reason": f"lease unavailable: {resource}"}
        if self._active >= self.max_workers:
            self.store.release_task_leases(job.task_id)
            queue_id = self.store.enqueue_managed(job.task_id, job.worker_id, job.adapter.name, "global capacity", self._payload(job))
            self.store.set_task_status(job.task_id, "WAITING_RESOURCE")
            with self._lock: self._pending.append(job)
            return {"status": "QUEUED", "queue_id": queue_id, "reason": "global capacity"}
        self._active += 1
        queue_id = self.store.enqueue_managed(job.task_id, job.worker_id, job.adapter.name, "scheduled", self._payload(job))
        self.store.mark_managed_started(queue_id)
        future = self.pool.submit(self._run, queue_id, job)
        return {"status": "STARTED", "queue_id": queue_id, "future": future}

    def _run(self, queue_id, job):
        worker_store = Store(self.store.path)
        try:
            return run_worker(worker_store, job.task_id, job.worker_id, job.adapter, job.prompt, job.cwd,
                              profile=job.profile, authority=job.authority, context=job.context,
                              conversation_id=job.conversation_id, resume_session_id=job.resume_session_id,
                              resume_from_task_id=job.resume_from_task_id, context_checkpoint=job.context_checkpoint,
                              preserve_resume_snapshot=job.preserve_resume_snapshot)
        finally:
            worker_store.close()
            self.store.mark_managed_finished(queue_id)
            with self._lock:
                self._active -= 1
                pending = self._pending.pop(0) if self._pending and self._active < self.max_workers else None
            if pending is not None:
                self.submit(pending)

    def run_sync(self, job: ManagedJob):
        result = self.submit(job)
        if result["status"] != "STARTED":
            raise RuntimeError(result["reason"])
        return result["future"].result()

    def close(self):
        self.pool.shutdown(wait=True)
