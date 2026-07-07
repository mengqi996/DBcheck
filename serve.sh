#!/usr/bin/env bash
# serve.sh — start the DBCheck frontend static server (threading).
#
# Why threading: `python3 -m http.server` is single-threaded by default,
# and a stuck keep-alive browser connection will block every later request.
# ThreadingHTTPServer handles each request on its own thread.
#
# Usage:
#   ./serve.sh                # port 8080, bind 127.0.0.1
#   ./serve.sh 9000           # custom port
#   HOST=0.0.0.0 ./serve.sh   # bind all interfaces (LAN access)
#
# Backend is expected at the API_BASE the frontend was built against;
# we read it from index.html and print a hint.

set -euo pipefail

PORT="${1:-8080}"
HOST="${HOST:-127.0.0.1}"

# Run from the script's directory so the relative path to index.html works
# regardless of where the user invoked the script from.
cd "$(dirname "$0")"

API_BASE="$(grep -oE 'http://localhost:[0-9]+' index.html | head -1 || true)"
API_BASE="${API_BASE:-http://localhost:8000}"

cat <<EOF
>>> DBCheck frontend
>>>   URL:      http://${HOST}:${PORT}/
>>>   Backend:  ${API_BASE}    (must be running separately)
>>>   Stop:     Ctrl+C
EOF

exec python3 - <<PY
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
host, port = "${HOST}", ${PORT}
print(f"ThreadingHTTPServer listening on {host}:{port}", flush=True)
ThreadingHTTPServer((host, port), SimpleHTTPRequestHandler).serve_forever()
PY