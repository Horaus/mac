# Safety and permissions

The control plane is local-first and intentionally explicit:

- Worker edits happen in a separate Git worktree.
- A worker result enters `REVIEW`; only the supervisor can accept it.
- Validation commands are recorded and must pass before `Supervisor.accept` integrates a result.
- Provider commands inherit the OS user permissions of the process. Use a dedicated project directory and provider sandbox flags such as Codex `--sandbox workspace-write`.
- The MCP facade has no network listener, authentication, or multi-user isolation. Do not expose its stdio bridge over a socket without adding an authenticated wrapper.
- Prompts, provider output and validation output may contain sensitive data; SQLite state should be protected with normal filesystem permissions.
