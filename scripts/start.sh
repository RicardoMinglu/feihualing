#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f .env ]]; then set -a; source .env; set +a; fi
if [[ ! -x .venv/bin/python ]]; then echo '请先执行 scripts/setup.sh'; exit 1; fi
if [[ ! -f "${FEIHUA_DATA_DIR:-data}/poetry.sqlite3" ]]; then echo '请先在私有数据目录提供兼容诗库，步骤见 README.md'; exit 1; fi
exec .venv/bin/python -m uvicorn app.main:app --host "${FEIHUA_HOST:-127.0.0.1}" --port "${FEIHUA_PORT:-8765}" --workers 1 --no-access-log
