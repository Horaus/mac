#!/usr/bin/env bash
set -euo pipefail
[[ "${1:-}" == "--yes" ]] || { echo "Usage: $0 --yes" >&2; exit 2; }
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
rm -rf "$ROOT/.venv" "$ROOT/.agent-control-plane"
rm -f "$HOME/.mac/bin/mac" "$HOME/.local/bin/mac"
echo "MAC runtime, state and launchers removed. Source files were kept."
