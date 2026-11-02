#!/usr/bin/env sh
# Start the Wristle listener on macOS or Linux.
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "Setting up Python environment (first run only)..."
  case " $* " in
    *" --voxtype "*) REQ=requirements-voxtype.txt ;;
    *) REQ=requirements.txt ;;
  esac
  python3 -m venv .venv && .venv/bin/pip install -r "$REQ"
fi
exec .venv/bin/python wristle.py "$@"
