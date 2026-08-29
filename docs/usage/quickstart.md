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

For a supervisor-facing structured interface, run:

```bash
python3 -m agent_control_plane mcp \
  --state /path/to/project/.agent-control-plane/state.sqlite3
```

The MCP process uses newline-delimited JSON-RPC on stdin/stdout. It is intended for a local trusted supervisor process, not an unauthenticated network service.
