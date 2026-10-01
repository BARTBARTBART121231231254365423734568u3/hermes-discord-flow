#!/usr/bin/env python3
"""hermes-ochtendrapport.py [--dry-run]: the workflow side's morning report in WORKFLOW → #ochtendrapport.

Systemd --user timer hermes-ochtendrapport.timer (flow config tijden.ochtendrapport, default 07:30 in the
configured time zone); one message with one ping per day (owner decision 30-09). No model. Sources:
- ~/.hermes/state/health.json (hermes-health.py, every 30 min): checks that fail now, since when;
- ~/.hermes/state/regressie.json (nightly check, e.g. flow-controle.py): N/M OK and the failing checks;
- ~/.hermes/state/zelf-opgelost.jsonl: what the ops session fixed itself since the last report (mode "opstart");
- the workflow inbox (flow config paden.workflow_inbox) *.md: open workflow items;
- the incident log (flow config paden.incidentenlog, optional): on Monday the weekly incident overview (count,
  impact, how many a check found before the owner, and the trend versus the week before);
- the mode file (paden.workflow_modus, default modus.standaard): "opstart" or "rapport". In "rapport" every problem
  gets its proposed fix (teksten.ochtendrapport.voorstellen); from "vraag_vanaf" the report asks to decide on
  switching modes in the ops session.
State: ~/.hermes/state/ochtendrapport.json (last report time, used for the "zelf opgelost" window).
"""
import json
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discord_post as dp  # noqa: E402
import flow_config as fc  # noqa: E402

H = fc.HOME
NL = fc.tz()
STATE = H / "state" / "ochtendrapport.json"
INCIDENTS = fc.optional_path("incidentenlog")
T = fc.get("teksten.ochtendrapport")
PROPOSALS = T["voorstellen"]  # rapport mode: the proposed fix per failing health check


def load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def hhmm(ts):
    return datetime.fromtimestamp(float(ts), NL).strftime("%d-%m %H:%M") if ts else "?"


def health_lines(mode):
    h = load(H / "state" / "health.json", None)
    if not h:
        return ["- gezondheid: nog geen gegevens (hermes-health.py heeft nog niet gedraaid)"]
    checks = h.get("checks", h)
    bad = [(n, c) for n, c in checks.items() if isinstance(c, dict) and not c.get("ok", True)]
    if not bad:
        return [f"- gezondheid: alles OK (laatste controle {hhmm(h.get('last_run') or h.get('at'))})"]
    out = []
    for name, c in bad:
        line = f"- ❗ {name}: {str(c.get('detail', ''))[:140]} (sinds {hhmm(c.get('since'))})"
        if mode == "rapport":
            line += f" → voorstel: {PROPOSALS.get(name, 'uitzoeken in de ops-sessie')}"
        out.append(line)
    return out


def regression_lines():
    r = load(H / "state" / "regressie.json", None)
    if not r:
        return ["- nachtelijke regressie: nog geen resultaat"]
    checks = r.get("checks", [])
    ok = sum(1 for c in checks if c.get("ok"))
    lines = [f"- nachtelijke regressie ({r.get('mode', '?')}): {ok}/{len(checks)} OK"]
    lines += [f"  - ❗ {c.get('name')}: {str(c.get('detail', ''))[:140]}" for c in checks if not c.get("ok")]
    return lines


def self_fixed(since):
    out = []
    try:
        for line in (H / "state" / "zelf-opgelost.jsonl").read_text().splitlines():
            e = json.loads(line)
            if float(e.get("at", 0)) > since:
                out.append(f"- 🔧 {hhmm(e['at'])}: {e.get('wat', '')[:160]} (test: {e.get('test', '?')[:60]})")
    except (OSError, ValueError):
        pass
    return out


def inbox_summary():
    """One line: teksten.ochtendrapport.inbox_regel (X open, oldest date) or inbox_leeg."""
    items = sorted(p.name for p in fc.path("workflow_inbox").glob("*.md") if p.name != "LEESMIJ.md")
    if not items:
        return fc.text("ochtendrapport.inbox_leeg")
    m = re.match(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})-", items[0])
    oldest = f"{m.group(3)}-{m.group(2)} {m.group(4)}:{m.group(5)}" if m else items[0][:13]
    return fc.text("ochtendrapport.inbox_regel", aantal=len(items), oudste=oldest)


def inbox_lines():
    items = sorted(p for p in fc.path("workflow_inbox").glob("*.md") if p.name != "LEESMIJ.md")
    out = []
    for p in items:
        title = next((l[2:].strip() for l in p.read_text(encoding="utf-8", errors="replace").splitlines()
                      if l.startswith("# ")), p.name)
        out.append(f"- 📥 {p.name[:13]} {title[:110]}")
    return out


def week_overview(today):
    if INCIDENTS is None:
        return ["- incidentenlogboek niet ingesteld (paden.incidentenlog)"]
    try:
        rows = [l for l in INCIDENTS.read_text().splitlines() if l.startswith("| 20")]
    except OSError:
        return ["- incidentenlogboek niet gevonden"]

    def parse(l):
        c = [x.strip() for x in l.strip("|").split("|")]
        try:
            d = date.fromisoformat(c[0])
            impact = int("".join(ch for ch in c[2].split()[0] if ch.isdigit()) or 0)
        except (ValueError, IndexError):
            return None
        return d, impact, c[4].lower(), c[5].lower()
    items = [x for x in (parse(r) for r in rows) if x]
    this = [x for x in items if today - timedelta(days=7) <= x[0] < today]
    prev = [x for x in items if today - timedelta(days=14) <= x[0] < today - timedelta(days=7)]
    by_check = sum(1 for x in this if fc.owner_name().lower() not in x[2])
    with_check = sum(1 for x in this if x[3].startswith("ja"))
    trend = "gelijk" if len(this) == len(prev) else ("minder" if len(this) < len(prev) else "meer")
    return [f"- vorige week: {len(this)} incidenten ({trend} dan de week ervoor: {len(prev)}), samen {sum(x[1] for x in this)} min impact",
            f"- door een controle of het team gevonden (niet door {fc.owner_name()}): {by_check}/{len(this)}; met een nieuwe controle erbij: {with_check}/{len(this)}"]


def main():
    dry = "--dry-run" in sys.argv
    now = time.time()
    today = datetime.now(NL).date()
    modus = load(fc.path("workflow_modus"), {"modus": fc.get("modus.standaard")})
    mode = modus.get("modus", fc.get("modus.standaard"))
    state = load(STATE, {})
    K = T["koppen"]
    parts = [fc.text("ochtendrapport.kop", datum=f"{today:%d-%m}", modus=mode)]
    parts += ["", K["gezondheid"]] + health_lines(mode)
    parts += ["", K["nacht"]] + regression_lines()
    fixed = self_fixed(float(state.get("last", now - 24 * 3600)))  # own fixes (opstart) and approved inbox steps (both modes)
    parts += ["", K["opgelost"]] + (fixed or ["- niets"])
    inbox = inbox_lines()
    parts += ["", K["inbox"], inbox_summary()] + inbox
    if today.weekday() == 0:
        parts += ["", K["week"]] + week_overview(today)
    ask = modus.get("vraag_vanaf")
    if mode == "opstart" and ask and today >= date.fromisoformat(ask):
        parts += ["", fc.text("ochtendrapport.modusvraag", tot=modus.get("opstart_tot"))]
    text = "\n".join(parts)
    if len(text) > 1900:
        text = text[:1880] + "\n… (ingekort)"
    if dry:
        print(text)
        return 0
    ch = dp.channels()
    if "ochtendrapport" not in ch:
        print("kanaal #ochtendrapport ontbreekt in discord.json", file=sys.stderr)
        return 1
    dp.send(ch["ochtendrapport"], text, ping=True)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"last": now}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
