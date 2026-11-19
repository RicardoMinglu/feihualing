#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if command -v uv >/dev/null 2>&1; then
  uv venv --python 3.12 .venv
  uv pip install --python .venv/bin/python -r requirements.lock
else
  python3.12 -m venv .venv
  .venv/bin/python -m pip install -r requirements.lock
fi
echo '依赖已安装。诗库的下载命令见 README.md。'
