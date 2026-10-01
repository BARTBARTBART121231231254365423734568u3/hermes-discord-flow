#!/bin/sh
# Planned gateway restart (workflow side): refuses while kanban workers run, writes the marker so
# gateway-watch.py stays silent, then restarts. Workers run in their own systemd scope and survive a gateway
# restart (checked 30-09 and 01-10): --scoped restarts anyway when EVERY running worker process is in its own
# hermes-worker-kanban-*.scope (drain-restart.py uses it after its 20-minute wait). --force only with akkoord.
set -e
H="${HERMES_HOME:-$HOME/.hermes}"
UNIT=$(python3 "$(dirname "$0")/flow_config.py" get gateway.unit 2>/dev/null || echo hermes-gateway.service)
running=$(python3 - "$H" <<'EOF'
import sqlite3, sys
from pathlib import Path
home = Path(sys.argv[1]); total = 0
for db in list((home / "kanban" / "boards").glob("*/kanban.db")) + [home / "kanban.db"]:
    if db.exists():
        total += sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'running'").fetchone()[0]
print(total)
EOF
)
scoped_ok() {  # every running card has a worker process, all in their own scope (not in the gateway cgroup)
  python3 - "$H" "$UNIT" <<'EOF'
import sqlite3, subprocess, sys
from pathlib import Path
home, unit = Path(sys.argv[1]), sys.argv[2]; ok = True
for db in (home / "kanban" / "boards").glob("*/kanban.db"):
    for (tid,) in sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute("SELECT id FROM tasks WHERE status = 'running'"):
        pids = subprocess.run(["pgrep", "-f", f"work kanban task {tid}"], capture_output=True, text=True).stdout.split()
        cgs = [Path(f"/proc/{p}/cgroup").read_text() if Path(f"/proc/{p}/cgroup").exists() else "" for p in pids]
        ok = ok and bool(pids) and all("/hermes-worker-kanban-" in c and unit not in c for c in cgs)
sys.exit(0 if ok else 1)
EOF
}
if [ "$running" != "0" ] && [ "$1" = "--scoped" ] && ! scoped_ok; then
  echo "niet herstart: $running worker(s) actief en niet allemaal in een eigen scope" >&2
  exit 1
fi
if [ "$running" != "0" ] && [ "$1" != "--force" ] && [ "$1" != "--scoped" ]; then
  echo "niet herstart: $running kanban-worker(s) actief" >&2
  exit 1
fi
mkdir -p "$H/state"
printf '{"at": %s, "by": "gateway-planned-restart.sh"}\n' "$(date +%s)" > "$H/state/gateway-planned-restart.json"
systemctl --user restart "$UNIT"
echo "gateway herstart (gepland)"
