#!/usr/bin/env bash
set -euo pipefail
REPO_URL="${ACP_REPO_URL:-https://github.com/Horaus/multi-agent-control.git}"
TARGET="${1:-multi-agent-control}"
if [ -e "$TARGET/.git" ]; then
  git -C "$TARGET" pull --ff-only
else
  git clone "$REPO_URL" "$TARGET"
fi
cd "$TARGET"
DATA_ROOT="$HOME/.mac/projects/$(basename "$PWD")"
mkdir -p "$HOME/.mac/projects"
if [ -d .agent-control-plane ] && [ ! -L .agent-control-plane ] && [ ! -e "$DATA_ROOT" ]; then mv .agent-control-plane "$DATA_ROOT"; fi
if [ ! -e .agent-control-plane ]; then ln -s "$DATA_ROOT" .agent-control-plane; fi
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
acp --project . init
if [ ! -f .agent-control-plane/config.json ]; then
  acp --project . setup
else
  echo "Giữ nguyên cấu hình MAC hiện có. Mở 'mac' để chỉnh nếu cần."
fi
mkdir -p "$HOME/.local/bin"
ln -sfn "$PWD/mac" "$HOME/.local/bin/mac"
if [ -d /opt/homebrew/bin ] && [ -w /opt/homebrew/bin ]; then ln -sfn "$PWD/mac" /opt/homebrew/bin/mac; fi
if [ -d /usr/local/bin ] && [ -w /usr/local/bin ]; then ln -sfn "$PWD/mac" /usr/local/bin/mac; fi
echo
echo "Đã cài lệnh global: mac"
echo "Mở MAC từ bất kỳ đâu bằng: mac"
case ":${PATH}:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "Nếu mac chưa được nhận, mở terminal mới hoặc thêm ~/.local/bin vào PATH." ;;
esac
