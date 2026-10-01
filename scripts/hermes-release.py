#!/usr/bin/env python3
"""hermes-release.py --notitie "<wat er veranderde>" [--dry-run]: publish a bundle of workflow changes.

Wijzigingsdiscipline (owner decision 30-09): workflow changes go live as a numbered release, never ad hoc.
1. Runs the quick check (flow config release.controle_commando, e.g. `flow-controle.py --quick`); it writes
   state/regressie.json. Red → stop, exit 1, nothing is published.
2. Release number YYYY-MM-DD.N (state ~/.hermes/state/releases.json).
3. Optional sync (release.sync_commando, e.g. a repo sync with a secret scan; a problem → exit 1; empty = skipped).
   A sync that prints JSON reports problems as {"problems": [...]}.
4. One silent line in WORKFLOW → #releases (teksten.release): "🚀 <nr> — <notitie> (regressie N/M OK)".
Command placeholders: {python} (this interpreter), {scripts} (this folder), {nr}, {notitie}.
Emergency fixes may go live directly, but get a release the same day. ``--dry-run`` runs the check and prints
what it would publish.
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

H = fc.HOME
STATE = H / "state" / "releases.json"
NL = fc.tz()
SCRIPTS = Path(__file__).resolve().parent


def command(key, **values):
    """``release.<key>`` with its placeholders filled in ([] = skip)."""
    values = {"python": sys.executable, "scripts": str(SCRIPTS), **values}
    return [str(part).format(**values) for part in fc.get(f"release.{key}") or []]


def main():
    args = sys.argv[1:]
    dry = "--dry-run" in args
    if "--notitie" not in args or args.index("--notitie") + 1 >= len(args):
        sys.exit('gebruik: hermes-release.py --notitie "<wat er veranderde>" [--dry-run]')
    note = args[args.index("--notitie") + 1].strip()
    check = command("controle_commando")
    r = subprocess.run(check, capture_output=True, text=True, timeout=1800) if check else None
    reg = {}
    try:
        reg = json.loads((H / "state" / "regressie.json").read_text())
    except (OSError, ValueError):
        pass
    checks = reg.get("checks", [])
    score = f"{sum(1 for c in checks if c.get('ok'))}/{len(checks)}"
    if r is not None and r.returncode != 0:
        print(f"GEEN release: snelle regressie rood ({score})")
        for c in checks:
            if not c.get("ok"):
                print(f"- {c.get('name')}: {str(c.get('detail', ''))[:200]}")
        return 1
    releases = json.loads(STATE.read_text()) if STATE.exists() else []
    today = datetime.now(NL).strftime("%Y-%m-%d")
    nr = f"{today}.{sum(1 for x in releases if x['nr'].startswith(today)) + 1}"
    line = fc.text("release", nr=nr, notitie=note, score=score)
    if dry:
        print("DRY-RUN:", line)
        return 0
    sync_cmd = command("sync_commando", nr=nr, notitie=note)
    if sync_cmd:
        sync = subprocess.run(sync_cmd, capture_output=True, text=True, timeout=1800)
        try:
            problems = json.loads(sync.stdout).get("problems", [])
        except ValueError:
            problems = [] if sync.returncode == 0 and not sync.stdout.strip() else \
                [sync.stderr.strip()[-300:] or "repo-sync gaf geen geldige uitvoer"]
        if sync.returncode != 0 or problems:
            print(f"GEEN release: repo-sync faalde: {problems}")
            return 1
    ch = dp.channels()
    if "releases" in ch:
        dp.send(ch["releases"], line, silent=True)
    releases.append({"nr": nr, "at": time.time(), "notitie": note, "regressie": score})
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(releases, ensure_ascii=False, indent=0))
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
