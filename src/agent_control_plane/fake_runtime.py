"""Deterministic fake runtime used for authority integration tests.

It models the state transitions of a CDP/Electron session without opening a
browser or touching a real profile. All mutations still pass through the
normal structured runtime authority boundary.
"""
from __future__ import annotations

from dataclasses import dataclass

from .authority import AuthorityPolicy, RuntimeOperation, RuntimeReport
from .service import authorize_runtime_action


@dataclass
class FakeRuntime:
    profile: str
    project_id: str = "fake-project"
    account_id: str = "fake-account"
    target_id: str = "fake-target"
    target_url: str = "about:blank"
    pid: int = 4242
    debug_port: int = 9222
    crashed: bool = False
    active_job: bool = False
    mutations: int = 0

    def report(self, action: str = "observe") -> RuntimeReport:
        return RuntimeReport(pid=self.pid, executable="fake-chrome", debug_port=self.debug_port,
                              user_data_dir=self.profile, target_id=self.target_id,
                              target_url=self.target_url, extension_id="fake-extension",
                              project_id=self.project_id, account_id=self.account_id,
                              active_job="job" if self.active_job else "",
                              intended_action=action, recovery_path="fake-reconnect",
                              allowed_target_ids=(self.target_id,), allowed_project_ids=(self.project_id,))

    def observe(self, store, policy: AuthorityPolicy, task_id: str, worker_id: str) -> RuntimeReport:
        authorize_runtime_action(store, policy, "browser.observe", self.report(),
                                 self.profile, task_id, worker_id, operation=RuntimeOperation.OBSERVE)
        return self.report()

    def mutate(self, store, policy: AuthorityPolicy, task_id: str, worker_id: str,
               operation: RuntimeOperation = RuntimeOperation.NAVIGATE) -> None:
        if self.crashed:
            raise RuntimeError("fake runtime is disconnected")
        authorize_runtime_action(store, policy, "browser.navigate", self.report(operation.value),
                                 self.profile, task_id, worker_id, self.active_job,
                                 self.target_id, self.project_id, operation=operation)
        self.mutations += 1

    def crash(self) -> None:
        self.crashed = True

    def reconnect(self) -> None:
        self.crashed = False
