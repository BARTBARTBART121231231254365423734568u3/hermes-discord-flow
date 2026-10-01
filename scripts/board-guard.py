#!/usr/bin/env python3
"""Board guard (cron, --no-agent, every 30 min). Layer 1 = this script, no model; layer 2 = the manager, only
when something is wrong. Checks the Team board for:

  vraag-zonder-post   blocked on the owner (title "Beslissing: …", needs_input or the question prefix) but no
                      #vragen post for the current block after 15 min
  zonder-reden        blocked without a reason (no needs_input, empty reason), not "beslissing manager"
  oude-vraag          blocked with a "Wacht op …" wait state, but the board still shows an owner question
                      (the latest run's summary)
  ready-zonder-eigenaar  ready without assignee for > 30 min ("workflow: …" cards are for the workflow side)
  te-lang-running     the current run is running for more than 3 hours
  zonder-project      open card without project (except "workflow: …" cards)
  kleurplaat-onduidelijk  the builder blocked with "kleurplaat onduidelijk: …" (no waiting time)
  verkeerd-maximum    a "te groot (N regels … max X)" block whose X is not the limit in the kleurplaat's "Maximale
                      grootte" line (the builder invented his own limit, 01-10)
  beslissing-manager  blocked with "beslissing manager" for more than 30 min
  blokkade-voor-manager  blocked with needs_input but the reason is no owner question (e.g. "worker preflight: …");
                      team-questions.py leaves it alone since 01-10, so the manager must solve it
  inloggegevens-in-kleurplaat  an open card's body contains a credential pattern in a URL (":$PGPASSWORD@",
                      ":***@"): builders copy it with the redacted *** (incident 01-10); use the test wrapper
  afhankelijkheid-zonder-reden  an open card waits on an open predecessor (task_links) without a line naming that
                      predecessor with the reason (TEAM.md "Afhankelijkheden"); not for review/-99/designer/security
                      cards or links from a plan/intake/decision card; cards created since DEPS_SINCE
  beslissing-zonder-vervolg  an intake or decision card ("Intake …", "Beslissing: …") went to done more than
                      15 min ago without a follow-up: no child card (``--parent``), no comment/summary
                      "Vervolg: t_…" naming an existing card, and no "Geen actie nodig: …" (cards done since
                      VERVOLG_SINCE, the moment this rule started)
  project-zonder-stap a project with STATUS ACTIEF or GEPAUZEERD has no open card at all (so also no open
                      question in #vragen: every question hangs on an open card), and its project file has no
                      line "GEEN VOLGENDE STAP: <reden>" (only on the owner's decision)
  geen-fasenplan      GEPAUZEERD project with a finished intake card, but no docs/PHASES.md in the repo (working
                      tree or any git branch) and no open card that writes it ("F0-00", "Fasenplan", "PHASES")

Projects with STATUS ACTIEF or GEPAUZEERD (zonder-project: every open card). When there are anomalies that were
not handed to the manager in the last 6 hours, the manager is woken silently in a CLI session with the list
(no Discord output); he fixes the project side himself, and turns a workflow fault into a "workflow: …" card
(no message; SOUL). An anomaly that is still there 6 hours after the manager got it is reported
once in #meldingen. Blocks that wait on a manager decision (kleurplaat-onduidelijk, verkeerd-maximum,
beslissing-manager) go before his own plan work: the manager is woken again every hour and after 2 hours there is
one action message in #meldingen (owner decision 01-10, after a pilot run waited 7.5 hours). State in the event register (discord-events.json): "guard:<kaart>:<soort>".
Personal values (board, roles, thresholds, texts, the allowed model override) come from the flow config.
``--dry-run`` lists the anomalies and changes nothing. Log: ~/.hermes/logs/board-guard.log.
"""
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discord_post as dp  # noqa: E402
import flow_config as fc  # noqa: E402
from team_projects import HOME, active_projects, boards, kanban, project_of  # noqa: E402

D = fc.get("drempels")
REPEAT = int(D["escalatie_uur"] * 3600)
VERVOLG_SINCE = int(fc.get("bordbewaking.regels_sinds.vervolg") or 0)  # follow-up rule in force (older decisions are not checked)
VERVOLG_GRACE = 15 * 60
UNPOSTED_AFTER = int(D["vraag_zonder_post_min"] * 60)
READY_AFTER = int(D["ready_zonder_eigenaar_min"] * 60)
RUNNING_TOO_LONG = int(D["te_lang_running_uur"] * 3600)
LOG = HOME / "logs" / "board-guard.log"
HERMES = fc.hermes_bin()
NL = fc.tz()
BOARD = fc.get("bord.naam")
OWNER = fc.owner_name()
PREFIX = fc.question_prefix().lower().rstrip(":")
BUILDER = fc.get("rollen.bouwer")
CHECKERS = set(fc.get("rollen.controleurs"))
ALLOWED_OVERRIDES = set(fc.get("bordbewaking.toegestane_model_override") or [])


def _reason(conn, task_id):
    ev = conn.execute("SELECT id, payload, created_at FROM task_events WHERE task_id = ? AND kind = 'blocked' "
                      "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if not ev:
        return None, "", 0
    try:
        reason = (json.loads(ev["payload"]) or {}).get("reason", "") if ev["payload"] else ""
    except (ValueError, TypeError):
        reason = ""
    return ev["id"], (reason or "").strip(), ev["created_at"]


FOLLOW_UP = re.compile(r"^\s*\**\s*vervolg(?:kaart(?:en)?)?\s*\**\s*:.*?\b(t_[0-9a-f]{8})\b", re.I | re.M)
NO_ACTION = re.compile(r"^\s*\**\s*geen actie nodig\s*\**\s*:\s*\S", re.I | re.M)
DECISION = re.compile(r"^\s*(intake\b|beslissing\s*:)", re.I)
PLAN_CARD = re.compile(r"\bF0-00\b|fasenplan|PHASES", re.I)
NO_NEXT_STEP = re.compile(r"^\s*GEEN VOLGENDE STAP:\s*\S", re.M)
PLAN_CODE = re.compile(r"^\s*(F\d+[A-Z]?)-00\b")
KLEURPLAAT = re.compile(r"^#\s*Kleurplaat\b", re.M | re.I)
PLACEHOLDER = re.compile(r"kleurplaat\s+(?:wordt|volgt)\b.*?(?:geschreven|nog)|plaatshouder", re.I)
UNAVAILABLE = re.compile(r"(?i)quota|rate.?limit|\b429\b|\b503\b|overloaded|unavailable|capacity|usage limit|exhausted|"
                         r"insufficient|credits|no available|not available")
CODER_CAP = int((fc.get("rollen.max_per_rol") or {}).get(BUILDER, 1))  # dispatcher caps (TEAM.md "Parallel werken")
HOST_CAP = int(fc.get("rollen.max_totaal"))
IDLE_AFTER = 30 * 60
REQUIRED_SECTIONS = tuple(fc.get("bordbewaking.verplichte_kleurplaat_secties"))  # kleurplaat template sections
SECTIONS_SINCE = int(fc.get("bordbewaking.regels_sinds.kleurplaat_secties") or 0)  # only cards created after the rule are held back
HOLD_REASON = "Wacht op: kleurplaat onvolledig"
REVIEW_PRIORITY = int(D["review_prioriteit"])  # review, herreview and end-check cards go before new build cards
REVIEW_WAIT = int(D["review_wacht_min"] * 60)
REVIEWISH = re.compile(r"^\s*F\d+[A-Z]?-(?:\d{2}R|99)\b|^\s*Review\b|^\s*Herreview\b", re.I)
MAX_LINES = re.compile(r"Maximale grootte[^\n]*?(\d{2,4})\s*regels", re.I)
DEFAULT_MAX_LINES = int(D["kleurplaat_max_regels"])
OVER_LIMIT = 1.2  # > 20% over the kleurplaat limit (TEAM.md "Maximale grootte vooraf")
TOO_BIG = re.compile(r"te groot\s*\(\s*~?(\d{2,5})\s*regels?[^)]*?\bmax(?:imaal|imum)?\.?\s*~?(\d{2,5})", re.I)
MANAGER_KINDS = {"kleurplaat-onduidelijk", "verkeerd-maximum", "beslissing-manager", "blokkade-voor-manager"}  # before his own plan work
MANAGER_REWAKE = int(D["manager_opnieuw_wekken_min"] * 60)
MANAGER_ESCALATE = int(D["manager_escalatie_uur"] * 3600)
DECISION_WAIT = int(D["beslissing_manager_wacht_min"] * 60)
CRED_IN_URL = re.compile(r"://\$?\{?\w+\}?:(?:\$\{?\w+\}?|\*\*\*)@")
DEPS_SINCE = int(fc.get("bordbewaking.regels_sinds.afhankelijkheden") or 0)  # dependency-reason rule in force
DEP_EXEMPT_ASSIGNEES = CHECKERS


def diff_lines(paths, branch):
    """Changed lines (added+removed) of a task branch against the project's staging branch, or None."""
    for repo in paths:
        try:
            out = subprocess.run(["git", "-C", repo, "diff", "--shortstat", f"origin/staging...{branch}"],
                                 capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        nums = re.findall(r"(\d+) (?:insertion|deletion)", out)
        if out.strip():
            return sum(int(n) for n in nums)
    return None


def dependency_checks(conn, t, title):
    """Open predecessors of ``t`` that the card does not explain ("Afhankelijk van: <id> — <waarom>")."""
    if t["created_at"] < DEPS_SINCE or t["assignee"] in DEP_EXEMPT_ASSIGNEES or REVIEWISH.match(title):
        return []
    text = (t["body"] or "") + "\n" + "\n".join(
        r[0] or "" for r in conn.execute("SELECT body FROM task_comments WHERE task_id = ?", (t["id"],)))
    missing = []
    for p in conn.execute("SELECT t.id, t.title, t.status FROM task_links l JOIN tasks t ON t.id = l.parent_id "
                          "WHERE l.child_id = ? AND t.status NOT IN ('done', 'archived')", (t["id"],)):
        ptitle = p["title"] or ""
        if PLAN_CODE.match(ptitle) or DECISION.match(ptitle) or PLAN_CARD.search(ptitle):
            continue
        explained = any(len(re.sub(r"[\W_]+", " ", line.split(p["id"], 1)[1]).strip()) >= 10
                        for line in text.splitlines() if p["id"] in line)
        if not explained:
            missing.append(p["id"])
    return missing


def has_follow_up(conn, t):
    """True when a done intake/decision card points at its next step (see the docstring)."""
    if conn.execute("SELECT 1 FROM task_links l JOIN tasks c ON c.id = l.child_id WHERE l.parent_id = ?",
                    (t["id"],)).fetchone():
        return True
    texts = [r[0] or "" for r in conn.execute("SELECT body FROM task_comments WHERE task_id = ?", (t["id"],))]
    texts += [r[0] or "" for r in conn.execute("SELECT summary FROM task_runs WHERE task_id = ?", (t["id"],))]
    texts.append(t["result"] or "")
    for text in texts:
        if NO_ACTION.search(text):
            return True
        for m in FOLLOW_UP.finditer(text):
            if m.group(1) != t["id"] and conn.execute("SELECT 1 FROM tasks WHERE id = ?", (m.group(1),)).fetchone():
                return True
    return False


def has_phases(paths):
    for path in paths:
        repo = Path(path)
        if (repo / "docs" / "PHASES.md").exists():
            return True
        try:
            out = subprocess.run(["git", "-C", str(repo), "rev-list", "--all", "-n", "1", "--", "docs/PHASES.md"],
                                 capture_output=True, text=True, timeout=60).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        if out:
            return True
    return False


def plan_has_cards(conn, code):
    """A done plan card (<code>-00) is only followed up when its tasks exist as cards: at least one build card
    <code>[A-Z]-NN and an end check <code>[A-Z]-99. F0-00 (fasenplan) is followed by the first block, F1…"""
    titles = [r[0] or "" for r in conn.execute("SELECT title FROM tasks WHERE status != 'archived'")]
    if code == "F0":
        return any(re.match(r"^\s*F1[A-Z]?-\d{2}\b", t) for t in titles)
    build = any(re.match(rf"^\s*{code}[A-Z]?-(?!00\b|99\b)\d{{2}}\b", t) for t in titles)
    end = any(re.match(rf"^\s*{code}[A-Z]?-99\b", t) for t in titles)
    return build and end


def decision_checks(conn, projects, intake_done, now):
    """beslissing-zonder-vervolg / plan-zonder-kaarten on done cards of one board; collects finished intakes
    and done F0-00 cards (fasenplan approved)."""
    found = []
    for t in conn.execute("SELECT id, title, status, project_id, workspace_path, completed_at, result FROM tasks "
                          "WHERE status = 'done'"):
        slug = project_of(t, projects)
        if not slug:
            continue
        plan = PLAN_CODE.match(t["title"] or "")
        if plan:  # every plan card, also from before the rule (owner decision 30-09: check all approved plans)
            if plan.group(1) == "F0":
                intake_done.add(("f0-done", slug))
            if now - (t["completed_at"] or now) >= VERVOLG_GRACE and not plan_has_cards(conn, plan.group(1)):
                found.append({"id": t["id"], "title": t["title"], "project": slug, "kind": "plan-zonder-kaarten",
                              "why": f"plan {plan.group(1)}-00 is af, maar de taken staan niet als kaarten op het bord "
                                     f"(bouwkaarten {plan.group(1)}…-NN met afhankelijkheden en een -99-eindcontrole); "
                                     "maak ze nu, zonder opnieuw toestemming te vragen"})
            continue
        if not DECISION.match(t["title"] or ""):
            continue
        if (t["title"] or "").strip().lower().startswith("intake"):
            intake_done.add(slug)
        done_at = t["completed_at"] or 0
        if done_at >= VERVOLG_SINCE and now - done_at >= VERVOLG_GRACE and not has_follow_up(conn, t):
            found.append({"id": t["id"], "title": t["title"], "project": slug, "kind": "beslissing-zonder-vervolg",
                          "why": "afgerond zonder vervolgkaart en zonder 'Geen actie nodig: …'; maak de "
                                 "vervolgkaart (--parent) of zet 'Vervolg: <id>' / 'Geen actie nodig: <reden>'"})
    return found


def project_checks(projects, open_cards, intake_done, work=None, capacity=None):
    """Project-level anomalies (id "project:<slug>"), over the open cards of all boards."""
    found = []
    for slug, p in projects.items():
        cards = open_cards.get(slug, [])
        title = f"Project {p['name']} ({p['status']})"
        if p["status"] == "GEPAUZEERD" and ("f0-done", slug) in intake_done:
            found.append({"id": f"project:{slug}", "title": title, "project": slug, "kind": "akkoord-zonder-actief",
                          "why": "het fasenplan (F0-00) is af en goedgekeurd, maar het project staat nog op GEPAUZEERD; "
                                 "zet STATUS: ACTIEF en maak de kaarten van het eerste blok (incl. -99)"})
        if (p["status"] == "ACTIEF" and capacity and capacity.get("free") and not (work or {}).get(slug)
                and not NO_NEXT_STEP.search(p["file"].read_text(encoding="utf-8", errors="replace"))):
            found.append({"id": f"project:{slug}", "title": title, "project": slug, "kind": "stilstaande-capaciteit",
                          "why": "een coderplek is al 30 min vrij en dit project heeft geen kaart in ready/running/review; "
                                 "zet het volgende werk uit het goedgekeurde fasenplan klaar (zonder opnieuw te vragen)"})
        if not cards and not NO_NEXT_STEP.search(p["file"].read_text(encoding="utf-8", errors="replace")):
            found.append({"id": f"project:{slug}", "title": title, "project": slug, "kind": "project-zonder-stap",
                          "why": "geen open kaart en geen open vraag in #vragen; niemand werkt eraan. Maak de "
                                 f"volgende kaart (na een intake: 'F0-00 Fasenplan schrijven + akkoord {OWNER}')"})
        if p["status"] == "GEPAUZEERD" and slug in intake_done and p["paths"] \
                and not any(PLAN_CARD.search(c) for c in cards) and not has_phases(p["paths"]):
            found.append({"id": f"project:{slug}", "title": title, "project": slug, "kind": "geen-fasenplan",
                          "why": "intake is afgerond, maar docs/PHASES.md ontbreekt en geen kaart schrijft hem; "
                                 f"maak 'F0-00 Fasenplan schrijven + akkoord {OWNER}'"})
    return found


def anomalies(now):
    projects = active_projects(("ACTIEF", "GEPAUZEERD"))
    found = []
    global holds, bumps
    holds, bumps = [], []
    open_cards, intake_done, work = {}, set(), {}
    running = {"total": 0, "coder": 0, "last_end": 0}
    for board, db in boards():
        conn = kanban(db)
        for r in conn.execute("SELECT assignee, COUNT(*) AS n FROM tasks WHERE status = 'running' GROUP BY assignee"):
            running["total"] += r["n"]
            running["coder"] += r["n"] if r["assignee"] == BUILDER else 0
        running["last_end"] = max(running["last_end"],
                                  conn.execute("SELECT MAX(ended_at) FROM task_runs").fetchone()[0] or 0)
        for t in conn.execute("SELECT id, title, status, assignee, block_kind, project_id, workspace_path, created_at, "
                              "body, last_failure_error, branch_name, model_override, priority FROM tasks WHERE status NOT IN ('done', 'archived')"):
            title = t["title"] or ""
            workflow = title.lower().startswith("workflow:")
            slug = project_of(t, projects)

            def add(kind, why):
                found.append({"id": t["id"], "title": title, "project": slug or "-", "kind": kind, "why": why})

            if workflow:
                add("workflow-kaart", "workflowpunten staan niet op het bord: zet dit als bestand in "
                    "~/.hermes/workflow-inbox/ (zie LEESMIJ.md) en archiveer de kaart")
                continue
            if not t["project_id"]:
                add("zonder-project", "open kaart zonder project")
            if not slug:
                continue
            open_cards.setdefault(slug, []).append(title)
            if t["status"] in ("ready", "running", "review"):
                work[slug] = True
            if (t["status"] in ("ready", "review") and (t["priority"] or 0) < REVIEW_PRIORITY
                    and (t["assignee"] in CHECKERS or t["status"] == "review"
                         or REVIEWISH.match(title))):
                moved = conn.execute("SELECT MAX(created_at) FROM task_events WHERE task_id = ? AND kind NOT IN "
                                     "('commented', 'heartbeat')", (t["id"],)).fetchone()[0] or t["created_at"]
                if now - moved > REVIEW_WAIT:
                    bumps.append(t["id"])
            if t["model_override"] and t["model_override"] not in ALLOWED_OVERRIDES:  # only the escalation model (TEAM.md)
                add("model-override", f"kaart heeft model-override '{t['model_override']}': die geldt ook voor de review; "
                    f"haal hem weg (hermes kanban --board {BOARD} set-model <id> none)")
            if t["status"] == "triage":
                last = conn.execute("SELECT kind FROM task_events WHERE task_id = ? AND kind NOT IN ('commented', 'heartbeat') "
                                    "ORDER BY id DESC LIMIT 1", (t["id"],)).fetchone()
                if last and last["kind"] == "block_loop_detected":
                    add("lus-gedetecteerd", "Hermes zette de kaart na herhaalde blokkades in triage (niemand pakt hem op); "
                        "los de oorzaak op en zet hem terug (blokkeer met de echte wachtstand, of deblokkeer)")
            if t["status"] == "review" and t["branch_name"] and KLEURPLAAT.search(t["body"] or ""):
                m = MAX_LINES.search(t["body"] or "")
                limit = int(m.group(1)) if m else DEFAULT_MAX_LINES
                n = diff_lines(projects[slug]["paths"], t["branch_name"])
                if n and n > limit * OVER_LIMIT:
                    add("te-grote-diff", f"diff is {n} regels, kleurplaat staat ~{limit} toe; splitsen vóór verdere review "
                        "(TEAM.md 'Maximale grootte vooraf'), niet achteraf accepteren")
            if t["assignee"] == BUILDER and t["status"] in ("todo", "ready", "blocked") and t["created_at"] >= SECTIONS_SINCE:
                body = t["body"] or ""
                missing = [sec for sec in REQUIRED_SECTIONS if not re.search(rf"\*\*{sec}\b", body)]
                if KLEURPLAAT.search(body) and not PLACEHOLDER.search(body) and missing:
                    add("kleurplaat-onvolledig", f"kleurplaat mist de verplichte sectie(s) {', '.join(missing)}; vul aan "
                        "(edit --body) en deblokkeer; de bordbewaking houdt de kaart tot dan tegen")
                    if t["status"] in ("todo", "ready"):
                        holds.append((t["id"], f"{HOLD_REASON} (mist {', '.join(missing)}) – bordbewaking"))
            if t["assignee"] == BUILDER and t["status"] in ("todo", "ready"):
                body = t["body"] or ""
                if not KLEURPLAAT.search(body) or PLACEHOLDER.search(body):
                    add("kleurplaat-ontbreekt", f"bouwkaart staat op {t['status']} zonder volledige kleurplaat; "
                        "schrijf nu de kleurplaat (edit --body)")
            if CRED_IN_URL.search(t["body"] or ""):
                add("inloggegevens-in-kleurplaat", "de kleurplaat bevat een commando met inloggegevens in een URL "
                    "(bijv. ':$PGPASSWORD@'); bouwers nemen dat over als '***'. Vervang het door het wrapper-commando uit "
                    "het projectbestand (bijv. `~/.hermes/bin/hermes-testdb pnpm verify`) met edit --body")
            missing = dependency_checks(conn, t, title) if t["status"] in ("todo", "ready", "blocked") else []
            if missing:
                add("afhankelijkheid-zonder-reden", f"wacht op {', '.join(missing)} zonder reden op de kaart; zet per "
                    "voorganger '- **Afhankelijk van:** <id> — <welke code, data of besluit>' in de body (edit --body), "
                    "of haal de link weg als hij niet echt nodig is (unlink)")
            if t["status"] == "blocked":
                ev, reason, since = _reason(conn, t["id"])
                r = reason.lower()
                wait, decision = r.startswith("wacht op"), r.startswith("beslissing manager")
                asks = title.lower().startswith("beslissing") or t["block_kind"] == "needs_input" \
                    or r.startswith(PREFIX)
                last = conn.execute("SELECT kind FROM task_events WHERE task_id = ? AND kind != 'commented' "
                                    "ORDER BY id DESC LIMIT 1", (t["id"],)).fetchone()
                if last and last["kind"] == "gave_up":
                    if not UNAVAILABLE.search(t["last_failure_error"] or ""):  # a storing: stuck-check reports it once
                        add("mislukt-2x", "twee keer mislukt (geen storing): pas de escalatieregel toe (kleurplaat "
                            "herschrijven of overnemen), fout: " + (t["last_failure_error"] or "?")[:160])
                    continue
                if r.startswith("kleurplaat onduidelijk"):
                    big = TOO_BIG.search(reason)
                    m = MAX_LINES.search(t["body"] or "")
                    limit = int(m.group(1)) if m else DEFAULT_MAX_LINES
                    if big and int(big.group(2)) != limit:
                        n = int(big.group(1))
                        verdict = ("valt binnen de grens: deblokkeer en laat hem review aanvragen" if n <= limit * OVER_LIMIT
                                   else "is echt te groot: splitsen")
                        add("verkeerd-maximum", f"de bouwer rekent met max {big.group(2)}, maar de kleurplaat staat ~{limit} "
                            f"regels toe (+20% = {int(limit * OVER_LIMIT)}); {n} regels {verdict}. Gaat vóór je eigen planwerk")
                    else:
                        add("kleurplaat-onduidelijk", "de bouwer vraagt verduidelijking (gaat vóór je eigen planwerk): "
                            + reason[len("kleurplaat onduidelijk"):].strip(" :")[:200])
                    continue
                if decision and now - since >= DECISION_WAIT:
                    add("beslissing-manager", f"wacht al {int((now - since) // 60)} min op jouw beslissing (gaat vóór je "
                        "eigen planwerk; SOUL 'Vastlopen'): " + reason[:160])
                    continue
                if wait:
                    run = conn.execute("SELECT summary FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                                       (t["id"],)).fetchone()
                    if run and (run["summary"] or "").strip().lower().startswith(PREFIX):
                        add("oude-vraag", "bord toont nog een beantwoorde vraag; herschrijf de wachtstand "
                            "(kanban_block met 'Wacht op …') zodat het bord de echte stand laat zien")
                elif t["block_kind"] == "needs_input" and reason and not r.startswith(PREFIX) \
                        and not title.lower().startswith("beslissing"):
                    add("blokkade-voor-manager", "needs_input-blokkade die geen vraag aan de eigenaar is (gaat vóór je "
                        f"planwerk): '{reason[:140]}'. Los het op: deblokkeer, of blokkeer opnieuw met de echte reden "
                        "('beslissing manager: …' of 'Wacht op …'); alleen een echte vraag gaat in het vraagformat")
                elif asks and not decision:
                    if now - since >= UNPOSTED_AFTER and not (ev and dp.event_seen(f"{t['id']}:vraag:{ev}")):
                        add("vraag-zonder-post", "geblokkeerd zonder needs_input of zonder geldige vraag"
                            if t["block_kind"] != "needs_input" or not reason else "vraag staat niet in #vragen")
                elif not reason and not decision:
                    add("zonder-reden", "geblokkeerd zonder reden")
            elif t["status"] == "ready" and not t["assignee"] and now - t["created_at"] > READY_AFTER:
                add("ready-zonder-eigenaar", "ready zonder assignee; wijs toe of archiveer")
            elif t["status"] == "running":
                run = conn.execute("SELECT started_at FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                                   (t["id"],)).fetchone()
                if run and now - (run["started_at"] or now) > RUNNING_TOO_LONG:
                    add("te-lang-running", f"draait al {int((now - run['started_at']) / 3600)} uur in één run")
        found += decision_checks(conn, projects, intake_done, now)
    capacity = {"free": running["total"] < HOST_CAP and running["coder"] < CODER_CAP
                and now - running["last_end"] >= IDLE_AFTER}
    return found + project_checks(projects, open_cards, intake_done, work, capacity)


def escalation_text(a, age):
    hours = int(age // 3600)
    if a["kind"] not in MANAGER_KINDS:
        return fc.text("bordbewaking.escalatie", id=a["id"], titel=a["title"][:80], project=a["project"], waarom=a["why"],
                       uren=hours)
    ch = dp.channels()
    link = f"https://discord.com/channels/{ch.get('guild')}/{ch.get('chatlog')}"
    return fc.text("bordbewaking.escalatie_manager", id=a["id"], titel=a["title"][:80], project=a["project"], uren=hours,
                   link=link)


def wake_manager(items):
    lines = "\n".join(f"- {a['id']} [{a['project']}] {a['title'][:80]}: {a['kind']} — {a['why']}" for a in items)
    prompt = ("Bordbewaking (automatisch, van de workflowkant). Deze kaarten wijken af; los ze op volgens je SOUL "
              "('Bordbewaking'). Je zit in een CLI-sessie: post niets in Discord. Een workflowfout zet je als bestand in "
              "~/.hermes/workflow-inbox/ (LEESMIJ.md), nooit als kaart of melding. Blokkades die op jouw beslissing "
              "wachten (kleurplaat-onduidelijk, verkeerd-maximum, beslissing-manager) los je eerst op, vóór je eigen planwerk.\n"
              + lines)
    r = subprocess.run([HERMES, "chat", "-Q", "-q", prompt], capture_output=True, text=True, timeout=1800,
                       env={"HOME": str(Path.home()), "PATH": f"{Path.home()}/.local/bin:/usr/bin:/bin"})
    return r.returncode, (r.stdout or "")[-1500:]


def log(line):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(f"{datetime.now(NL).isoformat(timespec='seconds')} {line}\n")


def main():
    now = time.time()
    found = anomalies(now)
    if "--dry-run" in sys.argv and holds:
        print("zou tegenhouden: " + ", ".join(t for t, _r in holds))
    if "--dry-run" in sys.argv and bumps:
        print("zou voorrang geven (prioriteit 10): " + ", ".join(bumps))
    if "--dry-run" in sys.argv:  # read-only: no lock, so it never waits on a running guard
        for a in found:
            print(f"{a['id']} [{a['project']}] {a['kind']}: {a['why']} | {a['title'][:70]}")
        print(f"{len(found)} afwijking(en)")
        return
    _lock = dp.single_instance("board-guard")  # noqa: F841 (held until exit)
    for task_id in bumps:  # review/herreview/end-check waiting > 15 min: before new build cards
        r = subprocess.run([HERMES, "kanban", "--board", BOARD, "edit", task_id, "--priority", str(REVIEW_PRIORITY)],
                           capture_output=True, text=True, timeout=120,
                           env={"HOME": str(Path.home()), "PATH": f"{Path.home()}/.local/bin:/usr/bin:/bin"})
        log(f"voorrang {task_id}: prioriteit {REVIEW_PRIORITY} (exit {r.returncode})")
    for task_id, reason in holds:  # hold back build cards with an incomplete kleurplaat before they start
        r = subprocess.run([HERMES, "kanban", "--board", BOARD, "block", task_id, reason], capture_output=True,
                           text=True, timeout=120, env={"HOME": str(Path.home()), "PATH": f"{Path.home()}/.local/bin:/usr/bin:/bin"})
        log(f"tegengehouden {task_id}: {reason} (exit {r.returncode})")
    due = []
    for a in found:
        info = dp.event_get(f"guard:{a['id']}:{a['kind']}") or {}
        manager = a["kind"] in MANAGER_KINDS
        if now - float(info.get("woken", 0)) >= (MANAGER_REWAKE if manager else REPEAT):
            due.append(a)
        if info.get("woken") and now - float(info["first"]) >= (MANAGER_ESCALATE if manager else REPEAT) \
                and not info.get("escalated"):
            msg = dp.send(dp.channels()["meldingen"], escalation_text(a, now - float(info["first"])), ping=True)
            dp.event_mark(f"guard:{a['id']}:{a['kind']}", escalated=now, message=msg["id"], channel=msg["channel_id"],
                          text=msg["content"], card=a["id"])
    if not due:
        return
    for a in due:
        info = dp.event_get(f"guard:{a['id']}:{a['kind']}") or {}
        dp.event_mark(f"guard:{a['id']}:{a['kind']}", woken=now, first=info.get("first", now), card=a["id"])
    log("manager gewekt: " + "; ".join(f"{a['id']}:{a['kind']}" for a in due))
    code, out = wake_manager(due)
    log(f"manager klaar (exit {code}): {out.strip().splitlines()[-1][:300] if out.strip() else '-'}")


if __name__ == "__main__":
    main()
