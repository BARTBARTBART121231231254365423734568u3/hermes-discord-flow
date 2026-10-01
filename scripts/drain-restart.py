#!/usr/bin/env python3
"""drain-restart.py [--before-restart "<cmd>"] [--max-wait-min N] | --restore [--failsafe] | --status

Planned gateway restart via drain (workflow side, owner rule 30-09): no new kanban cards start, running
workers finish, and only at 0 workers the gateway restarts. The dispatcher ALWAYS comes back on:

1. Drain: ``kanban.dispatch_profiles`` in ~/.hermes/config.yaml is set to '' (the dispatcher then claims no
   card; running workers are not touched; chat and cron keep working). The original line is saved in
   ~/.hermes/state/drain.json, config.yaml is backed up next to it.
2. Safety net: a transient systemd --user timer ``hermes-drain-failsafe`` runs ``--restore --failsafe``
   after 2 hours whatever happens to this script. If the drain is still active then, it restores the line
   and posts ONE message in #meldingen. If everything went fine it finds nothing to do and stays silent.
3. Wait for 0 running workers (max ``--max-wait-min``, default 20; owner decision 01-10: a long plan card held all work
   for an hour). A new worker spawn during the drain (the drain does not hold): restore, no restart, exit 1.
   Timeout with workers still running: when every running worker runs in its own systemd scope (they survive a
   gateway restart; checked 30-09 and 01-10) the gateway restarts anyway (``gateway-planned-restart.sh
   --scoped``) and the script checks afterwards that those workers still live. Otherwise: restore, no restart,
   and one retry is scheduled in 60 min (``--attempt``, at most 3 attempts).
4. Optional ``--before-restart`` command (e.g. fast-forward the live fork branch). Failure: restore, exit 1.
5. ``gateway-planned-restart.sh`` (without --force; silent in Discord).
6. Restore the original line (also when 4 or 5 failed), stop the safety timer, then check that the gateway
   is active and that the dispatcher claims again (a ready card starts, or no card was ready).
Start and end of a drain are posted silently in WORKFLOW → #gezondheid ("drain actief sinds …, tot uiterlijk …"),
so a paused dispatcher is not taken for a fault; team-status.py and hermes-health.py show the same.
Log: ~/.hermes/logs/drain-restart.log.
"""
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import flow_config as fc  # noqa: E402

H = fc.HOME
CONFIG = H / "config.yaml"
STATE = H / "state" / "drain.json"
LOG = H / "logs" / "drain-restart.log"
TIMER = "hermes-drain-failsafe"
FAILSAFE_AFTER = "2h"
KEY = "  dispatch_profiles:"
NL = fc.tz()
BOARD = fc.get("bord.naam")
UNIT = fc.get("gateway.unit")


def log(line):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(f"{datetime.now(NL).isoformat(timespec='seconds')} {line}\n")
    print(line)


def running_workers():
    total = 0
    for db in (H / "kanban" / "boards").glob("*/kanban.db"):
        total += sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'running'").fetchone()[0]
    return total


def worker_pids():
    """{task_id: [pids]} of the running cards' worker processes ("work kanban task <id>")."""
    out = {}
    for db in (H / "kanban" / "boards").glob("*/kanban.db"):
        for (tid,) in sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute(
                "SELECT id FROM tasks WHERE status = 'running'"):
            r = subprocess.run(["pgrep", "-f", f"work kanban task {tid}"], capture_output=True, text=True)
            out[tid] = [int(x) for x in r.stdout.split()]
    return out


def in_own_scope(pid):
    try:
        cg = Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return False
    return "/hermes-worker-kanban-" in cg and UNIT not in cg


def workers_survive(pids):
    """True when every running card has a worker process and all of them run in their own scope."""
    return bool(pids) and all(p and all(in_own_scope(x) for x in p) for p in pids.values())


def gezondheid(text):
    try:
        import discord_post as dp
        dp.send(dp.channels()["gezondheid"], text, ping=False)
    except Exception as exc:  # noqa: BLE001 (a missing post never blocks the restart)
        log(f"melding #gezondheid mislukt: {type(exc).__name__}")


def hhmm(ts):
    return datetime.fromtimestamp(ts, NL).strftime("%H:%M")


def runs_started_since(ts):
    """Worker spawns since ``ts``. Only 'spawned' events count: a block on a card without a worker (e.g. a
    manager's decision card) also writes a task_runs row, and that aborted the drain of 01-10."""
    total = 0
    for db in (H / "kanban" / "boards").glob("*/kanban.db"):
        total += sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute(
            "SELECT COUNT(*) FROM task_events WHERE kind = 'spawned' AND created_at > ?", (int(ts) + 5,)).fetchone()[0]
    return total


def ready_cards():
    db = H / "kanban" / "boards" / BOARD / "kanban.db"
    return [r[0] for r in sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute(
        "SELECT id FROM tasks WHERE status = 'ready' AND assignee IS NOT NULL")]


def kanban_line(text):
    """(index, line) of ``dispatch_profiles`` inside the top-level ``kanban:`` block."""
    lines = text.splitlines(keepends=True)
    inside = False
    for i, line in enumerate(lines):
        if not line.startswith((" ", "\t", "\n")) and line.strip():
            inside = line.rstrip() == "kanban:"
        elif inside and line.startswith(KEY):
            return i, line
    raise SystemExit("kanban.dispatch_profiles niet gevonden in config.yaml; niets veranderd")


def set_line(new_line):
    text = CONFIG.read_text()
    i, _old = kanban_line(text)
    lines = text.splitlines(keepends=True)
    lines[i] = new_line
    tmp = CONFIG.with_suffix(".yaml.drain-tmp")
    tmp.write_text("".join(lines))
    tmp.replace(CONFIG)


def drain(max_wait=20 * 60):
    if STATE.exists():
        raise SystemExit("er loopt al een drain (state/drain.json); eerst --restore")
    text = CONFIG.read_text()
    _i, original = kanban_line(text)
    backup = H / "state" / f"config.yaml.voor-drain-{int(time.time())}"
    shutil.copy2(CONFIG, backup)
    now = time.time()
    STATE.write_text(json.dumps({"line": original, "at": now, "until": now + max_wait, "backup": str(backup)}))
    set_line(f"{KEY} ''\n")
    subprocess.run(["systemctl", "--user", "stop", f"{TIMER}.timer"], capture_output=True)
    r = subprocess.run(["systemd-run", "--user", f"--unit={TIMER}", f"--on-active={FAILSAFE_AFTER}",
                        "--timer-property=AccuracySec=1min", "/usr/bin/python3", str(Path(__file__).resolve()),
                        "--restore", "--failsafe"], capture_output=True, text=True)
    if r.returncode != 0:
        restore(reason="vangnet-timer kon niet worden gezet")
        raise SystemExit(f"vangnet-timer mislukt, drain teruggedraaid: {r.stderr.strip()[:200]}")
    log(f"DRAIN aan: dispatcher claimt niets meer (was: {original.strip()}); vangnet over {FAILSAFE_AFTER}")
    gezondheid(fc.text("drain.actief", sinds=hhmm(now), tot=hhmm(now + max_wait)))


def restore(failsafe=False, reason=""):
    if not STATE.exists():
        if not failsafe:
            print("geen drain actief")
        return False
    info = json.loads(STATE.read_text())
    set_line(info["line"])
    STATE.unlink()
    if not failsafe:
        subprocess.run(["systemctl", "--user", "stop", f"{TIMER}.timer"], capture_output=True)
    log(f"DRAIN uit: {info['line'].strip()} teruggezet" + (f" ({reason})" if reason else "")
        + (" door het vangnet" if failsafe else ""))
    if not failsafe:
        gezondheid(fc.text("drain.klaar", reden=reason or "teruggezet"))
    if failsafe:
        import discord_post as dp
        since = datetime.fromtimestamp(info["at"], NL).strftime("%H:%M")
        dp.send(dp.channels()["meldingen"], fc.text("drain_vangnet", sinds=since), ping=True)
    return True


def dispatcher_claims(ready_before, wait_s=300):
    """True when the dispatcher claims again: a card that was ready starts running (or nothing was ready)."""
    if not ready_before:
        return None
    deadline = time.time() + wait_s
    db = H / "kanban" / "boards" / BOARD / "kanban.db"
    while time.time() < deadline:
        rows = sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute(
            f"SELECT id, status FROM tasks WHERE id IN ({','.join('?' * len(ready_before))})", ready_before).fetchall()
        started = [i for i, s in rows if s != "ready"]
        if started:
            return started
        time.sleep(15)
    return False


def retry(attempt, before):
    """Schedule one retry in 60 min (max 3 attempts); returns a log suffix."""
    if attempt >= 3:
        return "; geen nieuwe poging meer (3 pogingen)"
    cmd = ["systemd-run", "--user", f"--unit=hermes-drain-retry-{attempt + 1}", "--on-active=60min",
           "/usr/bin/python3", str(Path(__file__).resolve()), "--attempt", str(attempt + 1)]
    if before:
        cmd += ["--before-restart", before]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return f"; nieuwe poging {attempt + 1} over 60 min" if r.returncode == 0 else "; nieuwe poging plannen MISLUKT"


def main():
    args = sys.argv[1:]
    if "--status" in args:
        print(json.loads(STATE.read_text()) if STATE.exists() else "geen drain actief",
              f"| workers: {running_workers()}")
        return
    if "--restore" in args:
        restore(failsafe="--failsafe" in args)
        return
    before = args[args.index("--before-restart") + 1] if "--before-restart" in args else None
    max_wait = int(args[args.index("--max-wait-min") + 1]) * 60 if "--max-wait-min" in args else 20 * 60
    attempt = int(args[args.index("--attempt") + 1]) if "--attempt" in args else 1
    drain(max_wait)
    ok = False
    try:
        drained_at = time.time()
        deadline = drained_at + max_wait
        while running_workers() and time.time() < deadline:
            time.sleep(30)
            if runs_started_since(drained_at):  # the drain must stop new claims; if not, give up safely
                log("GEEN herstart: tijdens de drain startte toch een nieuwe run; drain werkt niet zoals verwacht")
                return 1
        scoped, pids = False, {}
        if running_workers():
            pids = worker_pids()
            if not workers_survive(pids):
                log(f"GEEN herstart: na {max_wait // 60} min nog {running_workers()} worker(s) actief, niet allemaal "
                    f"in een eigen scope ({pids})" + retry(attempt, before))
                return 1
            scoped = True
            log(f"na {max_wait // 60} min nog {len(pids)} worker(s) actief, alle in een eigen scope: herstart toch "
                f"({', '.join(pids)})")
        else:
            log("0 workers: herstart")
        if before:
            r = subprocess.run(before, shell=True, capture_output=True, text=True)
            log(f"voor de herstart: {before} → exit {r.returncode} {(r.stdout + r.stderr).strip()[-300:]}")
            if r.returncode != 0:
                return 1
        ready_before = ready_cards()
        r = subprocess.run([str(Path(__file__).resolve().parent / "gateway-planned-restart.sh")]
                           + (["--scoped"] if scoped else []), capture_output=True, text=True)
        log(f"herstart: exit {r.returncode} {(r.stdout + r.stderr).strip()[-200:]}")
        ok = r.returncode == 0
        if ok and scoped:
            time.sleep(10)
            alive = {t: [x for x in p if Path(f"/proc/{x}").exists()] for t, p in pids.items()}
            log("lopende workers na de herstart: " + ", ".join(f"{t} {'leeft' if a else 'WEG'}" for t, a in alive.items()))
    finally:
        restore(reason="na de herstart" if ok else "herstart niet gelukt")
    time.sleep(20)
    active = subprocess.run(["systemctl", "--user", "is-active", UNIT],
                            capture_output=True, text=True).stdout.strip()
    claims = dispatcher_claims(ready_before)
    log(f"controle: gateway {active}; dispatcher "
        + ("had geen klare kaarten om op te pakken" if claims is None else
           f"pakte weer op: {', '.join(claims)}" if claims else "pakte binnen 5 min GEEN klare kaart op"))
    return 0 if active == "active" and claims is not False else 1


if __name__ == "__main__":
    sys.exit(main() or 0)
