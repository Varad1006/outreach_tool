#!/usr/bin/env bash
# One-command start on macOS / Linux.
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
[ -f .env ] || { cp .env.example .env; echo "Created .env, fill in your keys, then run ./run.sh again."; exit 0; }
.venv/bin/python app.py
