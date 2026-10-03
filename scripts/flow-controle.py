#!/usr/bin/env python3
"""flow-controle.py [--quick]: nightly check of the Discord flow (generic version of a regression run).

Run by the systemd --user timer hermes-nachtcontrole.timer (flow config tijden.nachtcontrole) in full mode, and
with ``--quick`` by hermes-release.py before every workflow release. Read-only on live state: the dry runs
change nothing, nothing is posted to Discord.

Checks (each: name, ok/fout, one line of evidence, duration):
  1  py_compile of every *.py next to this script
  2  flow_config.py check (the local config is valid)
  3  team-questions.py --check: a valid question → 0, without ⭐ → 1, with a server path → 1
  4  dry runs exit 0: board-guard --dry-run, team-status, daily-summary-data, hermes-ochtendrapport --dry-run
Full mode only:
  5  per project with a ``verify`` command (flow config projecten): origin/<staging_branch> exported to a temp dir
     (git archive, nothing fetched), then the command runs there (20 min max). Include the install step in the
     command, e.g. "npm ci && npm run verify".

Result: ~/.hermes/state/regressie.json {started, finished, mode, ok, checks: [{name, ok, detail, seconds}]} (read by
the morning report and hermes-release.py), a short Dutch summary on stdout, exit 0 when everything is OK, else 1.
Log: ~/.hermes/logs/flow-controle.log.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from functools import partial
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import flow_config as fc  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent
RESULT = fc.HOME / "state" / "regressie.json"
LOG = fc.HOME / "logs" / "flow-controle.log"


def question(prefix):
    return f"""{prefix}
Wat speelt er: de nachtcontrole kijkt of een geldige vraag door de validatie komt.
Vraag: Welke kleur krijgt de knop?
Optie 1 ⭐ Aanbevolen: Groen
  Inhoud: de knop wordt groen.
  Voordeel: valt goed op.
  Nadeel: past minder bij het logo.
  Daarna: de bouwer past de kleur aan.
Optie 2: Blauw
  Inhoud: de knop wordt blauw.
  Voordeel: past bij het logo.
  Nadeel: valt minder op.
  Daarna: de bouwer past de kleur aan.
Waarom deze aanbeveling: 1) groen valt het meest op; 2) het is de gewone kleur voor een bevestiging. Kies liever optie 2 als de huisstijl zwaarder weegt.
Zonder antwoord: de knop blijft zoals hij is.
Details: alleen een test, er gebeurt niets."""


def run(cmd, timeout=240, input=None, cwd=None, shell=False):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input, cwd=cwd, shell=shell)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout na {timeout} s"


last = partial(fc.last_line, limit=200)


def check_compile():
    return fc.check_compile(sorted(SCRIPTS.glob("*.py")), limit=200)


def check_config():
    problems = fc.validate()
    return not problems, "config OK" if not problems else "; ".join(problems)


def check_questions():
    tq = str(SCRIPTS / "team-questions.py")
    q = question(fc.question_prefix())
    cases = (("geldig", q, 0), ("zonder ⭐", q.replace(" ⭐ Aanbevolen", ""), 1),
             ("serverpad", q.replace("Details: alleen", "Details: zie ~/x, alleen"), 1))
    bad = []
    for label, text, want in cases:
        rc, out, err = run([sys.executable, tq, "--check"], input=text, timeout=120)
        if rc != want:
            bad.append(f"{label}: exit {rc} (verwacht {want}; {last(out or err)})")
    return not bad, "--check: geldig→0, zonder ⭐→1, serverpad→1" if not bad else "; ".join(bad)


def dry(argv):
    def check():
        rc, out, err = run([sys.executable, str(SCRIPTS / argv[0]), *argv[1:]])
        return rc == 0, f"exit {rc}; laatste: {last(out if rc == 0 else (err or out))}"
    return check


def verify(slug, p):
    def check():
        repo, branch = p.get("repo"), p.get("staging_branch") or "staging"
        if not repo or not Path(repo).is_dir():
            return False, f"repo {repo} bestaat niet"
        tmp = Path(tempfile.mkdtemp(prefix=f"flow-controle-{slug}-"))
        try:
            arch = subprocess.run(["git", "-C", str(repo), "archive", f"origin/{branch}"], capture_output=True, timeout=300)
            if arch.returncode != 0:
                return False, f"git archive origin/{branch}: {last(arch.stderr.decode(errors='replace'))}"
            subprocess.run(["tar", "-x", "-C", str(tmp)], input=arch.stdout, check=True, timeout=300)
            rc, out, err = run(p["verify"], timeout=1200, cwd=tmp, shell=True)
            return rc == 0, f"{p['verify']}: exit {rc}; {last(out if rc == 0 else (err or out))}"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return check


def checks(full):
    items = [("1 py_compile scripts", check_compile), ("2 config", check_config),
             ("3 team-questions --check", check_questions),
             ("4 board-guard --dry-run", dry(["board-guard.py", "--dry-run"])),
             ("4 team-status", dry(["team-status.py"])),
             ("4 daily-summary-data", dry(["daily-summary-data.py"])),
             ("4 hermes-ochtendrapport --dry-run", dry(["hermes-ochtendrapport.py", "--dry-run"]))]
    if full:
        items += [(f"5 {slug} verify", verify(slug, p)) for slug, p in fc.projects().items()
                  if fc.is_set(p.get("verify")) and slug != "voorbeeld-project"]
    return items


def main():
    full = "--quick" not in sys.argv
    mode = "volledig" if full else "quick"
    started = time.time()
    results = []
    for name, fn in checks(full):
        t0 = time.time()
        try:
            ok, detail = fn()
        except Exception as e:  # noqa: BLE001 (one broken check must not stop the others)
            ok, detail = False, f"{type(e).__name__}: {e}"
        results.append({"name": name, "ok": bool(ok), "detail": detail[:300], "seconds": round(time.time() - t0, 1)})
    finished = time.time()
    stamp = lambda ts: datetime.fromtimestamp(ts, fc.tz()).isoformat(timespec="seconds")  # noqa: E731
    all_ok = all(r["ok"] for r in results)
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    tmp = RESULT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"started": stamp(started), "finished": stamp(finished), "mode": mode, "ok": all_ok,
                               "checks": results}, ensure_ascii=False, indent=1))
    tmp.replace(RESULT)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        for r in results:
            fh.write(f"{stamp(finished)} {'OK  ' if r['ok'] else 'FOUT'} {r['name']}: {r['detail']}\n")
    n_ok = sum(r["ok"] for r in results)
    print(f"Controle ({mode}): {n_ok}/{len(results)} OK in {int(finished - started)} s"
          + (" — alles in orde." if all_ok else ". Fout:"))
    for r in results:
        if not r["ok"]:
            print(f"- {r['name']}: {r['detail']}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    sys.exit(main())
