# Quickstart

```bash
python3 -m pip install -e .
acp --project /path/to/project init
acp --project /path/to/project task create --id feature --title 'Implement feature'
acp --project /path/to/project worker run --task-id feature --worker-id worker-a \
  --cwd /path/to/worktree --prompt 'Implement the feature'
acp --project /path/to/project validate --task-id feature --cwd /path/to/worktree pytest
acp --project /path/to/project accept feature
```

Run `mac`, open the **Workers** tab, and press `x` on a worker to configure its
model, Codex profile, reasoning effort, sandbox, and context policy. Blank
execution fields inherit provider defaults, so existing worker configuration
continues to behave as before. Provider account entitlement still determines
whether a selected model is available.

For a supervisor-facing structured interface, run:

```bash
python3 -m agent_control_plane mcp \
  --state /path/to/project/.agent-control-plane/state.sqlite3
```

The MCP process uses newline-delimited JSON-RPC on stdin/stdout. It is intended for a local trusted supervisor process, not an unauthenticated network service.

## Execution and compact results

Use `execution.mode` as the canonical authority field. Supported values are
`read-only`, `workspace-write`, `isolated_sandbox`, and `open_operator`.
Provider-specific `sandbox` belongs to the execution profile and must not be
used as a replacement for authority mode.

For a compact task result, call `status` with `task_id`,
`inspection_mode: result_only`, and an optional `byte_limit` from 512 to 16384.
The response excludes unrelated history, validation stdout, transcripts, and
activity dumps. Follow `evidence_id` only when deeper inspection is required.
