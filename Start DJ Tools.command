#!/bin/bash
cd "$(dirname "$0")"
PORT=8765
# stop an earlier DJ Tools server that is still listening on the default port
for pid in $(lsof -ti tcp:$PORT -sTCP:LISTEN 2>/dev/null); do
  if ps -p "$pid" -o command= | grep -q "webui.py"; then
    echo "Stopping the DJ Tools server already running on port $PORT (pid $pid)…"
    kill "$pid"; sleep 1
  else
    echo "Port $PORT is used by another program (pid $pid), not touching it."
  fi
done
if [ ! -x .venv/bin/python ]; then
  if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 is required. Install it from https://www.python.org/downloads/ and double-click this file again."
    read -n 1 -s -r -p "Press any key to close."; exit 1
  fi
  echo "First start: installing the libraries (about a minute)…"
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt || {
    echo "Install failed (see above). Is the internet connection on?"; rm -rf .venv
    read -n 1 -s -r -p "Press any key to close."; exit 1; }
fi
exec .venv/bin/python webui.py
