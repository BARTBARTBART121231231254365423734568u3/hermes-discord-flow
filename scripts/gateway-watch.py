#!/usr/bin/env python3
"""Gateway watcher (systemd --user timer hermes-gateway-watch.timer, every 2 min; independent of the
gateway, so it also works while the gateway is down). Posts to #meldingen with a ping only when:
- the gateway restarted UNEXPECTEDLY (crash with automatic restart, server reboot, or stopped and
  started without a planned-restart marker);
- the gateway has been down for more than 2 minutes; when it is back that message is edited to "✅ opgelost (HH:MM)
  — …" (dp.opgelost, event key "gateway-plat:<down since>"; no new message, no ping).
Planned restarts are silent: ``drain-restart.py --gepland`` writes the marker
~/.hermes/state/gateway-planned-restart.json first; a chat /restart leaves ~/.hermes/.restart_pending.json.
The first run records the current state silently. State: ~/.hermes/state/gateway-watch.json.
"""
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discord_post as dp  # noqa: E402
import flow_config as fc  # noqa: E402
from flow_config import HOME  # noqa: E402

UNIT = fc.get("gateway.unit")
STATE = HOME / "state" / "gateway-watch.json"
MARKER = HOME / "state" / "gateway-planned-restart.json"
CHAT_RESTART = HOME / ".restart_pending.json"
PLANNED_WINDOW = 15 * 60
DOWN_ALERT_AFTER = int(fc.get("drempels.gateway_plat_na_s"))


def unit_state() -> dict:
    out = subprocess.run(fc.systemctl_gateway("show", UNIT, "-p",
                          "ActiveState,SubState,InvocationID,NRestarts,ActiveEnterTimestamp"),
                         capture_output=True, text=True, timeout=30, check=True).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def planned(now: float) -> bool:
    """A fresh, unused planned-restart marker or chat /restart marker; the marker is marked used."""
    try:
        data = json.loads(MARKER.read_text())
        if not data.get("used") and now - float(data.get("at", 0)) < PLANNED_WINDOW:
            MARKER.write_text(json.dumps({**data, "used": True, "used_at": now}))
            return True
    except (OSError, ValueError):
        pass
    try:
        return now - float(json.loads(CHAT_RESTART.read_text()).get("requested_at", 0)) < PLANNED_WINDOW
    except (OSError, ValueError):
        return False


def hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts, fc.tz()).strftime("%H:%M")


def main():
    now = time.time()
    unit, boot = unit_state(), boot_id()
    prev = json.loads(STATE.read_text()) if STATE.exists() else None
    cur = {"invocation": unit.get("InvocationID", ""), "nrestarts": int(unit.get("NRestarts") or 0),
           "boot": boot, "down_since": None, "down_alerted": False}
    up = unit.get("ActiveState") in ("active", "reloading")
    if prev is None:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(cur))
        print("baseline vastgelegd")
        return
    if up:
        if prev.get("invocation") and cur["invocation"] != prev["invocation"]:
            if planned(now):
                print("geplande herstart: stil")
            else:
                if boot != prev.get("boot"):
                    why = "de server is opnieuw opgestart"
                elif cur["nrestarts"] > int(prev.get("nrestarts") or 0):
                    why = "hij crashte en systemd startte hem automatisch opnieuw"
                else:
                    why = "hij stopte en startte zonder geplande herstart"
                dp.send(dp.channels()["meldingen"], fc.text("gateway.onverwacht", waarom=why,
                                                            sinds=unit.get("ActiveEnterTimestamp", "?")), ping=True)
        if prev.get("down_alerted"):
            dp.opgelost("gateway-plat:", now)
    else:
        cur["invocation"] = prev.get("invocation", "")
        cur["nrestarts"] = int(prev.get("nrestarts") or 0)
        cur["down_since"] = prev.get("down_since") or now
        cur["down_alerted"] = bool(prev.get("down_alerted"))
        if not cur["down_alerted"] and now - cur["down_since"] > DOWN_ALERT_AFTER and not planned(now):
            dp.meld("meldingen", f"gateway-plat:{int(cur['down_since'])}", fc.text(
                "gateway.plat", sinds=hhmm(cur["down_since"]), status=f"{unit.get('ActiveState')}/{unit.get('SubState')}"),
                ping=True)
            cur["down_alerted"] = True
    STATE.write_text(json.dumps(cur))


if __name__ == "__main__":
    main()
