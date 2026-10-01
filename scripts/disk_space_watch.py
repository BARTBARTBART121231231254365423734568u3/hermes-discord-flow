#!/usr/bin/env python3
"""No-agent cron script: waarschuwt via Discord (#meldingen) als de schijfruimte onder de drempel zakt.

Watchdog-patroon: lege stdout = niets te doen (cron stuurt dan niets).
Alleen bij een echte toestandsverandering wordt een bericht geprint,
zodat de eigenaar geen spam krijgt bij elke run.

- Checkt / (root) op deze server
- Drempel: flow config drempels.schijf_min_gb (standaard 10 GB vrij); tekst: teksten.schijf
- State-bestand voorkomt herhaalde meldingen: alleen bij
  OK -> LAAG overgang, bij verdere daling >1 GB, of 1x per 24u als reminder
- Herstel (LAAG -> OK) stuurt bewust géén bericht (alleen gevraagd: melding bij <10GB)
"""
import json
import os
import shutil
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flow_config as fc  # noqa: E402

THRESHOLD_GB = float(fc.get("drempels.schijf_min_gb"))
STATE_DIR = str(fc.HOME / "state")
STATE_FILE = os.path.join(STATE_DIR, "disk_space_alert.json")
REMIND_INTERVAL_S = 24 * 3600
WORSEN_STEP_GB = 1.0
CHECK_PATH = "/"


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def main() -> None:
    try:
        total, used, free = shutil.disk_usage(CHECK_PATH)
    except Exception as e:
        # Schijf niet leesbaar: 1x melden, daarna stil tot herstel
        state = load_state()
        now = time.time()
        last_err = state.get("last_error_at", 0)
        if now - last_err < REMIND_INTERVAL_S and state.get("error_alerted"):
            return
        state["error_alerted"] = True
        state["last_error_at"] = now
        save_state(state)
        print(f"⚠️ Schijfcheck mislukt op {socket.gethostname()}: {e}")
        return

    free_gb = free / (1024 ** 3)
    total_gb = total / (1024 ** 3)
    used_pct = (used / total * 100) if total else 0
    host = socket.gethostname()
    now = time.time()
    state = load_state()

    if free_gb >= THRESHOLD_GB:
        # OK: state resetten, geen bericht (geen spam bij normaal)
        if state.get("alerted"):
            state = {}
            save_state(state)
        return

    # LAAG (<10 GB): bepalen of we moeten melden
    last_alert_at = state.get("last_alert_at", 0)
    last_alert_gb = state.get("last_alert_gb")
    alerted = state.get("alerted", False)

    should_alert = False
    if not alerted:
        should_alert = True  # OK -> LAAG overgang
    elif last_alert_gb is not None and (last_alert_gb - free_gb) >= WORSEN_STEP_GB:
        should_alert = True  # fors verder gedaald
    elif (now - last_alert_at) >= REMIND_INTERVAL_S:
        should_alert = True  # 24u reminder terwijl nog steeds laag

    if not should_alert:
        return

    state["alerted"] = True
    state["last_alert_at"] = now
    state["last_alert_gb"] = round(free_gb, 2)
    save_state(state)

    print(fc.text("schijf", host=host, vrij=free_gb, totaal=total_gb, procent=used_pct, drempel=THRESHOLD_GB))


if __name__ == "__main__":
    main()
