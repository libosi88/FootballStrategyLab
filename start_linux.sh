#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
runtime=python3
if [[ -x .venv/bin/python ]]; then runtime=.venv/bin/python; fi
"$runtime" -c 'import sys,struct; sys.exit(0 if (3,11)<=sys.version_info[:2]<(3,14) and struct.calcsize("P")==8 else "需要 64 位 Python 3.11–3.13；请配置兼容解释器后重试。")'
if [[ ! -x .venv/bin/python ]]; then python3 -m venv .venv; fi
.venv/bin/python -B -m pip install --only-binary=:all: --require-hashes -r installer.lock
.venv/bin/python -B -m pip install --only-binary=:all: --require-hashes -r requirements.lock
exec .venv/bin/python -B app.py serve
