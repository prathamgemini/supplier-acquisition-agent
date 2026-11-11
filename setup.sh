#!/usr/bin/env bash
# One-time setup for macOS / Linux.
set -e
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt
[ -f .env ] || { cp .env.example .env; echo "Created .env - open it and paste your Groq API key."; }
echo "Setup complete. Next: put your Groq key in .env, then run ./run.sh"
