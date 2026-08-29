from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .providers import ProviderAdapter, WorkerResult
from .service import run_worker
from .store import Store


@dataclass(frozen=True)
class Job:
    task_id: str
    worker_id: str
    adapter: ProviderAdapter
    prompt: str
    cwd: str | Path
    resources: tuple[tuple[str, str], ...] = ()


def run_concurrently(store: Store, jobs: list[Job], max_workers: int = 4) -> dict[str, WorkerResult | Exception]:
    """Dispatch ready jobs concurrently; deterministic gates happen before provider calls."""
    runnable: list[Job] = []
    results: dict[str, WorkerResult | Exception] = {}
    for job in jobs:
        if not store.dependencies_ready(job.task_id):
            store.set_task_status(job.task_id, "WAITING_DEPENDENCY")
            results[job.task_id] = RuntimeError("dependency not ready")
            continue
        blocked = False
        for resource, mode in job.resources:
            store.declare_resource_access(job.task_id, resource, mode)
            if not store.acquire(resource, job.task_id, job.worker_id, mode):
                blocked = True
                break
            store.consume_resource(job.task_id, resource)
        if blocked:
            # Roll back leases acquired earlier in this job; a multi-resource
            # request must be all-or-nothing to avoid partial-lock deadlocks.
            store.release_task_leases(job.task_id)
            store.set_task_status(job.task_id, "WAITING_RESOURCE")
            results[job.task_id] = RuntimeError("resource unavailable")
        else:
            runnable.append(job)
    def isolated_run(job: Job):
        # Each worker gets its own SQLite connection. This avoids sharing a
        # transaction-bound connection across threads while preserving one DB.
        worker_store = Store(store.path)
        try:
            return run_worker(worker_store, job.task_id, job.worker_id, job.adapter, job.prompt, job.cwd)
        finally:
            worker_store.close()

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        pending = {pool.submit(isolated_run, j): j for j in runnable}
        for future in as_completed(pending):
            job = pending[future]
            try:
                results[job.task_id] = future.result()
            except Exception as error:
                results[job.task_id] = error
    return results
