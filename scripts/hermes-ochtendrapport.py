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
- the team board (kanban/boards/team/kanban.db) and the kanban sessions of every profile (state.db), always via
  fc.connect_ro: a second, silent message "Teamcijfers" (24 h | 7 d: output, blockades, questions, usage) with a
  "Berekening" block: per figure its source/filter and a check that must hold (✓) or not (≠). Counts only.
State: ~/.hermes/state/ochtendrapport.json (last report time, used for the "zelf opgelost" window).
"""
import json
import subprocess
import statistics
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discord_post as dp  # noqa: E402
import flow_config as fc  # noqa: E402
import werkmap_groei  # noqa: E402

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


def subscription_lines():
    """Verbruik van elk abonnement (flow config abonnement.providers) via `hermes usage --json`;
    waarschuwing per abonnement als een weekvenster boven abonnement.krap_procent zit."""
    lines = []
    krap, stop = float(fc.get("abonnement.krap_procent") or 70), fc.get("abonnement.stop_procent") or 90
    for prov, naam in fc.abonnementen().items():
        windows = fc.verbruik(prov)
        if windows is None:
            lines.append(fc.text("ochtendrapport.abonnement_onbekend") + f" ({naam})")
            continue
        tight = False
        for label, pct, resets in windows:
            try:
                reset = datetime.fromisoformat(resets).astimezone(NL).strftime("%d-%m %H:%M")
            except (TypeError, ValueError):
                reset = "?"
            lines.append(fc.text("ochtendrapport.abonnement_regel", naam=naam, label=label, procent=round(pct), reset=reset))
            tight = tight or ("week" in str(label).lower() and pct >= krap)
        if tight:
            lines.append(fc.text("ochtendrapport.abonnement_krap", naam=naam, drempel=round(krap), stop=stop))
    return lines or [fc.text("ochtendrapport.abonnement_onbekend")]


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


def teller_lines(vorig):
    """Opruimteller (besluit eigenaar 03-10): vaste telling uit metingen/teller.py in de ops-map (de map van
    paden.incidentenlog) met de doelen van blok A."""
    if not INCIDENTS:
        return None, ["- teller niet te berekenen"]
    teller = INCIDENTS.parent / "metingen" / "teller.py"
    try:
        t = json.loads(subprocess.run([sys.executable, str(teller), "--json"],
                                      capture_output=True, text=True, timeout=120).stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None, ["- teller niet te berekenen"]
    pijl = lambda k: {True: "↓", False: "↑"}.get(t[k] < vorig[k], "") if t[k] != vorig.get(k, t[k]) else "="  # noqa: E731
    return t, ["- " + "; ".join(f"{k.replace('_', '-')} {t[k]}/{t['doelen'][k] or '–'}/{t['ochtend_0310'][k]} "
                                f"{'' if t['doelen'][k] is None else '✓' if t[k] <= t['doelen'][k] else '✗'}{pijl(k)}"
                                for k in ("scripts", "regels", "controles", "fork_patches")),
               f"- (nu/doel/03-10 ochtend; pijl t.o.v. vorig rapport; telling: {str(teller).replace(str(Path.home()), '~', 1)})"]


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


# --- Teamcijfers (blok D): alleen aantallen, nooit kaarttitels. Elk cijfer met berekening en controle. ---
BOARD = H / "kanban" / "boards" / fc.get("bord.naam") / "kanban.db"
PROFIEL_DB = {"default": H / "state.db", **{p: H / "profiles" / p / "state.db"
                                            for p in ("coder", "reviewer", "security", "designer")}}
LOST = ("unblocked", "completed", "archived", "promoted")  # wat een blokkade beëindigt


def _pct(xs, q):
    return sorted(xs)[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def _med(xs):
    return statistics.median(xs) if xs else None


def _js(t):
    try:
        return json.loads(t or "{}") or {}
    except ValueError:
        return {}


def koppel_sessies(runs, sessies, now):
    """{run_id: [sessie]}: sessie.started_at in [run.started_at-5, (run.ended_at of nu)+5] van hetzelfde profiel;
    runs van 0 seconden (preflight-stop, starten geen sessie) tellen niet mee.
    Bij parallelle runs (in volgorde van start): de vroegste run die nog geen sessie heeft, anders de laatst gestarte.
    Elke sessie hoort bij hooguit één run."""
    uit = {}
    for s in sorted(sessies, key=lambda s: s["started_at"]):
        kand = sorted((r for r in runs if r["profile"] == s["profile"] and (r["ended_at"] or now) > r["started_at"]
                       and r["started_at"] - 5 <= s["started_at"] <= (r["ended_at"] or now) + 5),
                      key=lambda r: r["started_at"])
        if kand:
            r = next((r for r in kand if r["id"] not in uit), kand[-1])
            uit.setdefault(r["id"], []).append(s)
    return uit


def teamcijfers(kb, sessies, now, dagen):
    """Cijfers over het venster [now - dagen, now]. kb: verbinding met het bord (fc.connect_ro of een test-db),
    sessies: [{id, profile, started_at, calls, tokens}] (source kanban)."""
    van = now - dagen * 86400
    taken = [dict(id=r[0], created_at=r[1], completed_at=r[2]) for r in kb.execute(
        "SELECT id, created_at, completed_at FROM tasks WHERE completed_at >= ? AND completed_at <= ?", (van, now))]
    ev = [dict(task_id=r[0], kind=r[1], p=_js(r[2]), at=r[3]) for r in kb.execute(
        "SELECT task_id, kind, payload, created_at FROM task_events WHERE created_at >= ? AND kind IN "
        "('blocked','block_loop_detected','completed','unblocked','archived','promoted')", (van,))]
    t0 = min([van] + [t["created_at"] for t in taken])
    runs = [dict(id=r[0], task_id=r[1], profile=r[2], started_at=r[3], ended_at=r[4], outcome=r[5], meta=_js(r[6]))
            for r in kb.execute("SELECT id, task_id, profile, started_at, ended_at, outcome, metadata FROM task_runs "
                                "WHERE COALESCE(ended_at, ?) >= ?", (now, t0))]
    park_comm = kb.execute("SELECT COUNT(*) FROM task_comments WHERE created_at >= ? AND body LIKE "
                           "'Geparkeerd door de herstartgrens%'", (van,)).fetchone()[0]
    c = {"dagen": dagen}
    klaar = {t["id"] for t in taken}
    c["klaar"], c["klaar_ev"] = len(klaar), len({e["task_id"] for e in ev if e["kind"] == "completed"})
    door = [(t["completed_at"] - t["created_at"]) / 3600 for t in taken]
    c["door_med"], c["door_p90"], c["door_n"] = _med(door), _pct(door, 0.9), len(door)
    inr = [r for r in runs if r["ended_at"] and van <= r["ended_at"] <= now]
    merges = [r["meta"]["merged_commit"] for r in inr if r["profile"] == "reviewer" and r["meta"].get("merged_commit")]
    c["merges"], c["merge_runs"] = len(set(merges)), len(merges)  # één merge kan twee kaarten afsluiten
    c["rev_klaar"] = sum(1 for r in inr if r["profile"] == "reviewer" and r["outcome"] == "completed")
    blok = [e for e in ev if e["kind"] == "blocked" and not e["p"].get("status_update")]
    duur, open_ = [], []
    for b in blok:
        nxt = min((e["at"] for e in ev if e["task_id"] == b["task_id"] and e["kind"] in LOST and e["at"] > b["at"]),
                  default=None)
        (duur.append((nxt - b["at"]) / 3600) if nxt else open_.append((now - b["at"]) / 3600))
    c.update(blok=len(blok), opgelost=len(duur), open=len(open_), los_med=_med(duur),
             lang=sum(1 for x in duur + open_ if x > 4))
    c["geparkeerd"] = sum(1 for e in ev if e["kind"] == "block_loop_detected" and "herstart_limit" in e["p"])
    c["park_comm"] = park_comm
    pre = fc.question_prefix().lower()
    vr = [e for e in blok if str(e["p"].get("reason") or "").strip().lower().startswith(pre)]
    c["vragen"], c["vragen_kaarten"] = len(vr), len({e["task_id"] for e in vr})
    last = {}  # open questions to the owner > 24 h: outside the board guard's escalation (owner 03-10), only here
    for tid, kind, payload, at in kb.execute("SELECT task_id, kind, payload, created_at FROM task_events WHERE kind IN "
                                             "('blocked','unblocked','completed','archived','promoted') ORDER BY created_at, rowid"):
        last[tid] = (kind, _js(payload), at)
    c["vraag_oud"] = sorted(t for t, (k, p, at) in last.items() if k == "blocked" and now - at > 86400
                            and str(p.get("reason") or "").strip().lower().startswith(pre))
    per_run = koppel_sessies(runs, [s for s in sessies if s["started_at"] >= t0 - 5], now)
    tok, calls, zonder = [], [], 0
    for tid in klaar:
        ss = [s for r in runs if r["task_id"] == tid for s in per_run.get(r["id"], [])]
        zonder += not ss
        if ss:
            tok.append(sum(s["tokens"] for s in ss))
            calls.append(sum(s["calls"] for s in ss))
    c.update(tok_med=_med(tok), tok_gem=sum(tok) / len(tok) if tok else None, calls_med=_med(calls),
             tok_n=len(tok), zonder_sessie=zonder)
    met_id = [(r, per_run.get(r["id"], [])) for r in runs if r["meta"].get("worker_session_id")]
    c["id_klopt"] = sum(1 for r, ss in met_id if r["meta"]["worker_session_id"] in {s["id"] for s in ss})
    c["id_n"] = len(met_id)
    coder = [r for r in inr if r["profile"] == "coder"]
    echt = [r for r in coder if r["ended_at"] > r["started_at"]]
    c.update(coder_alle=len(coder), coder_echt=len(echt), coder_blok=sum(1 for r in echt if r["outcome"] == "blocked"))
    return c


def lees_sessies(since):
    """Kanban-sessies van alle profielen sinds ``since``, altijd via fc.connect_ro (nooit direct)."""
    uit = []
    for prof, db in PROFIEL_DB.items():
        if not db.exists():
            continue
        for r in fc.connect_ro(db).execute(
                "SELECT id, started_at, api_call_count, COALESCE(input_tokens,0) + COALESCE(cache_read_tokens,0) "
                "+ COALESCE(output_tokens,0) FROM sessions WHERE source = 'kanban' AND started_at >= ?", (since,)):
            uit.append(dict(id=r[0], profile=prof, started_at=float(r[1]), calls=r[2] or 0, tokens=r[3] or 0))
    return uit


def _f(x, nd=1):
    return "–" if x is None else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))


_mln = lambda x: "–" if x is None else f"{x / 1e6:.1f} mln"  # noqa: E731
ok = lambda b: "✓" if b else "≠"  # noqa: E731


def teamcijfers_lines(d, w):
    """d = 24 uur, w = 7 dagen (dicts uit teamcijfers)."""
    vr = w["vragen"] / 7
    blokpct = lambda c: f"{100 * c['coder_blok'] / c['coder_echt']:.0f}%" if c["coder_echt"] else "–"  # noqa: E731
    return [
        "**Teamcijfers** (24 u | 7 d)",
        f"- kaarten klaar: {d['klaar']} | {w['klaar']}; merges naar staging: {d['merges']} | {w['merges']}",
        f"- doorlooptijd (7 d): mediaan {_f(w['door_med'])} u, p90 {_f(w['door_p90'])} u",
        f"- nieuwe blokkades: {d['blok']} | {w['blok']}; 7 d: opgelost na mediaan {_f(w['los_med'])} u; "
        f"> 4 u: {w['lang']}; nog open: {w['open']}",
        f"- geparkeerd door de herstartgrens: {d['geparkeerd']} | {w['geparkeerd']}",
        f"- vragen aan {fc.owner_name()}: {d['vragen']} | {vr:.1f}/dag (doel max 5/dag)"
        + (" ⚠️" if d["vragen"] > 5 or vr > 5 else "")
        + (f"; > 24 u open: {len(d['vraag_oud'])} ({', '.join(d['vraag_oud'][:5])})" if d.get("vraag_oud") else ""),
        f"- tokens per klare kaart (7 d): mediaan {_mln(w['tok_med'])}, gemiddeld {_mln(w['tok_gem'])}; "
        f"calls mediaan {_f(w['calls_med'], 0)}",
        f"- geblokkeerde coder-runs: {blokpct(d)} | {blokpct(w)} ({d['coder_blok']}/{d['coder_echt']} | "
        f"{w['coder_blok']}/{w['coder_echt']})",
        "",
        "**Berekening** (bron · filter · controle)",
        f"- klaar: tasks.completed_at in venster (ook later gearchiveerd) · controle: kaarten met event 'completed' {d['klaar_ev']} | "
        f"{w['klaar_ev']} {ok(d['klaar'] == d['klaar_ev'] and w['klaar'] == w['klaar_ev'])}",
        f"- merges: unieke merged_commit in reviewer-runs (ended_at in venster) · controle: uniek {w['merges']} ≤ "
        f"runs ermee {w['merge_runs']} ≤ klare reviewer-runs {w['rev_klaar']} "
        f"{ok(w['merges'] <= w['merge_runs'] <= w['rev_klaar'])}",
        f"- doorlooptijd: created_at→completed_at van de klare kaarten · controle: n={w['door_n']} = klaar, "
        f"p90 ≥ mediaan {ok(w['door_n'] == w['klaar'] and (w['door_p90'] or 0) >= (w['door_med'] or 0))}",
        f"- blokkades: event 'blocked' zonder status_update; opgelost = eerstvolgende {'/'.join(LOST)} · controle: "
        f"opgelost {w['opgelost']} + open {w['open']} = {w['blok']} {ok(w['opgelost'] + w['open'] == w['blok'])}",
        f"- geparkeerd: 'block_loop_detected' met herstart_limit · controle: parkeercommentaren {w['park_comm']} "
        f"{ok(w['park_comm'] == w['geparkeerd'])}",
        f"- vragen: blokkades met reden '{fc.question_prefix()}…' (7 d gedeeld door 7) · controle: op "
        f"{w['vragen_kaarten']} kaarten, ≤ blokkades {ok(w['vragen_kaarten'] <= w['vragen'] <= w['blok'])}",
        f"- tokens: in + cache + uit van kanban-sessies, gekoppeld aan runs op starttijd (±5 s, zelfde profiel) · "
        f"controle: {w['tok_n']}/{w['klaar']} kaarten met sessie, worker_session_id klopt {w['id_klopt']}/{w['id_n']} "
        f"(doel ≥ 95%) {ok(w['tok_n'] + w['zonder_sessie'] == w['klaar'] and w['id_klopt'] >= 0.95 * w['id_n'])}",
        f"- coder-runs: outcome blocked / runs met ended_at > started_at · controle: {w['coder_echt']} echt + "
        f"{w['coder_alle'] - w['coder_echt']} preflight-stops = {w['coder_alle']} "
        f"{ok(w['coder_blok'] <= w['coder_echt'] <= w['coder_alle'])}",
    ]


FABLE_STOP = 1791048204  # 03-10 ~18:43 UTC: besluit eigenaar, Fable eruit (telt op de Claude-weeklimiet)


def fable_sessies():
    """Aantal sessies op een Fable-model sinds FABLE_STOP, alle profielen en alle bronnen (moet 0 zijn)."""
    return sum(fc.connect_ro(db).execute("SELECT COUNT(*) FROM sessions WHERE started_at >= ? AND lower(model) LIKE "
                                         "'%fable%'", (FABLE_STOP,)).fetchone()[0] for db in PROFIEL_DB.values() if db.exists())


def team_section(now):
    try:
        kb = fc.connect_ro(BOARD)
        sessies = lees_sessies(now - 60 * 86400)
        fable = fable_sessies()
        return teamcijfers_lines(teamcijfers(kb, sessies, now, 1), teamcijfers(kb, sessies, now, 7)) + [
            f"- Fable-sessies sinds het besluit van 03-10: {fable} (moet 0 zijn){' ⚠️ fout: zoek uit welk profiel' if fable else ' ✓'}"
            " · bron: sessions.model van alle profielen, alle bronnen"]
    except Exception as e:  # noqa: BLE001 - het rapport gaat altijd door
        return ["**Teamcijfers**", f"- niet te berekenen: {type(e).__name__}: {str(e)[:120]}"]


def delen(text, maks=1900):
    """Splitst op lege regels (secties) in berichten van ≤ maks tekens; een te lange sectie op regels."""
    uit, cur = [], ""
    for blok in text.split("\n\n"):
        for stuk in ([blok] if len(blok) <= maks else blok.split("\n")):
            if cur and len(cur) + 2 + len(stuk) > maks:
                uit.append(cur)
                cur = ""
            cur = f"{cur}\n\n{stuk}" if cur and stuk.startswith("**") else (f"{cur}\n{stuk}" if cur else stuk[:maks])
    return uit + ([cur] if cur else [])


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
    teller, regels = teller_lines(load(STATE, {}).get("teller") or {})
    parts += ["", "**Opruimteller** (doel eind blok A)"] + regels
    subs = subscription_lines()
    if subs:
        parts += ["", fc.text("ochtendrapport.abonnement_kop")] + subs
    fixed = self_fixed(float(state.get("last", now - 24 * 3600)))  # own fixes (opstart) and approved inbox steps (both modes)
    backups = werkmap_groei.backup_summary(float(state.get("last", now - 24 * 3600)))
    if backups:
        fixed = fixed + [fc.text("ochtendrapport.backup_regel", weg=backups[0], bewaard=backups[1])]
    parts += ["", K["opgelost"]] + (fixed or ["- niets"])
    inbox = inbox_lines()
    parts += ["", K["inbox"], inbox_summary()] + inbox
    if today.weekday() == 0:
        parts += ["", K["week"]] + week_overview(today)
    ask = modus.get("vraag_vanaf")
    if mode == "opstart" and ask and today >= date.fromisoformat(ask):
        parts += ["", fc.text("ochtendrapport.modusvraag", tot=modus.get("opstart_tot"))]
    berichten = delen("\n".join(parts)) + delen("\n".join(team_section(now)))  # niets afkappen: op secties splitsen
    if dry:
        print("\n\n---\n".join(berichten))
        return 0
    ch = dp.channels()
    if "ochtendrapport" not in ch:
        print("kanaal #ochtendrapport ontbreekt in discord.json", file=sys.stderr)
        return 1
    for i, b in enumerate(berichten):
        dp.send(ch["ochtendrapport"], b, ping=i == 0, silent=i > 0)  # één ping per dag
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"last": now, "teller": {k: v for k, v in (teller or {}).items() if isinstance(v, int)}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
