#!/usr/bin/env python3
"""A stand-in for ``systemctl --user`` in the clean-install test: logs every call to $FAKE_SYSTEMCTL_LOG and
answers is-active/show for the gateway from $FAKE_SYSTEMCTL_STATE ({"ActiveState", "SubState", "InvocationID",
"NRestarts", "ActiveEnterTimestamp"}). Never touches the real systemd."""
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_SYSTEMCTL_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\n")
try:
    state = json.load(open(os.environ["FAKE_SYSTEMCTL_STATE"]))
except (OSError, ValueError, KeyError):
    state = {"ActiveState": "active", "SubState": "running", "InvocationID": "inv1", "NRestarts": "0"}
rest = [a for a in args if a != "--user"]
if rest[:1] == ["is-active"]:
    unit = rest[1] if len(rest) > 1 else ""
    print(state["ActiveState"] if "gateway" in unit else "inactive")
    sys.exit(0 if state["ActiveState"] == "active" or "gateway" not in unit else 3)
if rest[:1] == ["show"]:
    unit = rest[1] if len(rest) > 1 else ""
    props = state if "gateway" in unit else {"ActiveState": "inactive", "SubState": "dead", "Result": "success"}
    for k, v in props.items():
        print(f"{k}={v}")
    sys.exit(0)
if rest[:1] == ["list-units"] and "--type=scope" in rest:
    for line in state.get("scopes", []):  # worker scopes the test wants the health check to see
        print(line)
sys.exit(0)  # daemon-reload, enable, list-units (nothing else listed), …
