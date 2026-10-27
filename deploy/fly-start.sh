#!/bin/sh
# Load and index the item files on the volume (/data/incoming/) when they're new, then serve.
# Copy files up with `fly ssh sftp shell` (put items.xlsx /data/incoming/items.xlsx) and restart the machine.
# --no-proxy-headers: never take the client address from X-Forwarded-For (a visitor could claim 127.0.0.1 and pass
# as the local machine, skipping the login).
mkdir -p /data/incoming
if [ -z "$(ls -A /data/incoming 2>/dev/null)" ]; then
  echo "no item files in /data/incoming: using the demo item list"; cp /app/samples/demo-data/* /data/incoming/
fi
python - <<'PY'
import sys
from flexbulk.launch import needs_build
why = needs_build(False)
sys.exit(0 if not why else (print("building:", why) or 3))
PY
[ $? -eq 3 ] && python -m flexbulk.build
exec uvicorn flexbulk.server:app --host 0.0.0.0 --port 8080 --no-proxy-headers
