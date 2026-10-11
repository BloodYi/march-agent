#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo 'Create .venv and install requirements.txt first. / 请先创建 .venv 并安装 requirements.txt。' >&2
  exit 1
fi
export PATH="$HOME/.local/bin:$PATH"
exec .venv/bin/python run.py
