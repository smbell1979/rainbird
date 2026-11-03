#!/usr/bin/env bash
#
# Exit 1 (and say why) if a zone is watering or a run is queued; 0 if idle or
# the service isn't answering at all (nothing to interrupt then).
#
# Used before any switch of the running service: replacing it mid-run stops
# the run. Zones started from the app or the panel count too: the switch
# reconnects to the module, which serves one client at a time.

set -euo pipefail

status=$(curl -fsS --max-time 10 http://127.0.0.1:8710/api/status) || exit 0
python3 -c '
import json, sys
s = json.load(sys.stdin)
busy = []
if s.get("current"):
    busy.append("running " + s["current"]["name"])
if s.get("queued"):
    busy.append("%d run(s) queued" % len(s["queued"]))
if s.get("running"):
    busy.append("zone(s) %s on at the controller" % s["running"])
if busy:
    print("busy: " + "; ".join(busy), file=sys.stderr)
    sys.exit(1)
' <<<"$status"
