#!/usr/bin/env sh
# Start the Wristle listener on macOS or Linux.
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "Setting up Python environment (first run only)..."
  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
fi
exec .venv/bin/python wristle.py "$@"
