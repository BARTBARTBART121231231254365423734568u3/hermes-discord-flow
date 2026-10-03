#!/usr/bin/env python3
"""Board guard (cron, --no-agent, every 15 min; since 03-10 also the former kanban-stuck-check.py). Layer 1 = this
script, no model; layer 2 = the manager, only when something is wrong. Checks the Team board for:

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
  blokkade-voor-manager  blocked with needs_input but the reason is no owner question (e.g. "worker preflight: …");
                      team-questions.py leaves it alone since 01-10, so the manager must solve it
  blokkade / blokkade-vraag  one rule for every block of an ACTIEF project (owner decision 03-10). Owner from the
                      reason: the question prefix → the owner (already in #vragen: never escalated; the morning
                      report lists them after 24 h); "Wacht op … <kaart-id>" → the role of that card; else the
                      manager. No progress (the awaited card – else the card itself – is not running and had no run,
                      event or comment in the last hour; an open workflow-inbox item named in the reason counts as
                      progress) → after 30 min "blokkade" (wake text with a playbook per sort), after 2 h
                      "blokkade-vraag" (the manager asks the owner in #vragen with what was tried; workflow matters go
                      to the inbox). A reason with a date (ISO or dd-mm) that has not passed yet does not count.
                      Replaces "beslissing-manager", the 6-hour message and the stuck-check ping.
  wachtkring          2+ blocked cards whose reasons name each other's id: the card that goes first (reviewer before
                      security, else the oldest) is unblocked with a note, and the manager is told
  voorraad-laag       an ACTIEF project has < 3 coder cards that are ready or can start (todo with every parent
                      done) while a plan card (-00) or a todo manager card (not -99/review/decision) is open, and no
                      manager card of that project is running: plan the next 3 build cards now (max 1x per hour)
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

Projects with STATUS ACTIEF or GEPAUZEERD (zonder-project: every open card). The manager is woken silently in a CLI
session with the list (no Discord output); he fixes the project side himself, and turns a workflow fault into a
workflow-inbox file. Blocks, the cycle and the coder supply go before his own plan work: woken again every hour;
other anomalies every 6 hours. One wake-up per project per round with all its due items (most urgent first), at
least 30 min apart unless a new kleurplaat/maximum/cycle item came in, never while a manager card of it runs. Nothing of this goes to #meldingen any more. A card blocked on a workflow-inbox item
(file name in the reason) that is in workflow-inbox/afgehandeld/ is unblocked with a note.
#meldingen (with ping, ACTIEF projects, was kanban-stuck-check.py; one message per episode, discord-cleanup.py marks
it "✅ opgelost"): triage > 1 h; todo with every parent done and not started 30 min after the last one finished;
ready > 2 h while a worker slot for its assignee is free for 10+ min ("since" = the card's last non-comment event);
a card in the workflow-waits file (paden.workflow_waits, {"<task_id>": {"unit": "<unit>"}}) once its unit failed or
finished while the card is still blocked; runs that crashed, timed out or gave up (24 h) as "❌ Kaart mislukt"; a
builder that is unavailable (quota, 429…) as ONE message per 6 hours. State in the event register
(discord-events.json): "guard:<kaart>:<soort>", "<kaart>:vastgelopen:<status>:<sinds>", "<kaart>:mislukt:<event>".
Personal values (board, roles, thresholds, texts, the allowed model override) come from the flow config.
``--dry-run`` lists everything and changes nothing. Log: ~/.hermes/logs/board-guard.log.
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
from flow_config import HOME, active_projects, boards, kanban, project_of  # noqa: E402

D = fc.get("drempels")
REPEAT = int(D["opnieuw_wekken_uur"] * 3600)
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
MANAGER = fc.get("rollen.manager")
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
# A real placeholder only: "kleurplaat wordt/volgt … geschreven/nog", or a line that starts with "Plaatshouder" — not the
# word inside a normal sentence ("geen plaatshouderteksten" in Randgevallen gave a false alarm, 01-10).
PLACEHOLDER = re.compile(r"kleurplaat\s+(?:wordt|volgt)\b.*?(?:geschreven|nog)|^\s*[-*>]?\s*(?:\*\*)?plaatshouder\b", re.I | re.M)
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
SPECIFIC_BLOCKS = {"kleurplaat-onduidelijk", "verkeerd-maximum", "blokkade-voor-manager"}  # woken at once
MANAGER_KINDS = SPECIFIC_BLOCKS | {"blokkade", "blokkade-vraag", "wachtkring", "voorraad-laag"}  # before his plan work
MANAGER_REWAKE = int(D["manager_opnieuw_wekken_min"] * 60)
BLOCK_WAKE = int(D["blokkade_wek_min"] * 60)
BLOCK_ASK = int(D["blokkade_vraag_uur"] * 3600)
SUPPLY_MIN = int(D["coder_voorraad_min"])
BUNDLE_GAP = 30 * 60  # one wake-up per project per round, at least this long apart …
DIRECT = {"kleurplaat-onduidelijk", "verkeerd-maximum", "wachtkring"}  # … unless one of these is new
URGENCY = ["wachtkring", "kleurplaat-onduidelijk", "verkeerd-maximum", "blokkade-voor-manager", "blokkade-vraag",
           "blokkade", "mislukt-2x", "voorraad-laag"]  # order in the wake text; the rest after these
DATE = re.compile(r"\b(?:(20\d\d)-(\d\d)-(\d\d)|(\d{1,2})-(\d{1,2})(?:-(20\d\d))?)\b")
CARD_ID = re.compile(r"\bt_[0-9a-f]{8}\b")
INBOX = fc.path("workflow_inbox")
INBOX_ITEM = re.compile(r"\b(\d{8}-\d{4}-[A-Za-z0-9-]*[A-Za-z0-9])(?:\.md)?")
ACCESS = re.compile(r"(?i)toegang|login|inlog|account|wachtwoord|credential|token|rechten|permission|access|database|"
                    r"\bdb\b|omgeving|environment|preflight|worktree|server|railway|secret|vapid")
NO_PROGRESS = ("blocked", "commented", "heartbeat", "reprioritized", "dependency_wait", "block_loop_detected",
               "rate_limited", "respawn_guarded", "spawn_failed", "gave_up")  # events that are no progress
PLAYBOOK = {  # R2 (owner 03-10): what the manager does per sort of block, short, in the wake text
    "kleurplaat": "kleurplaat onduidelijk: vul de kleurplaat aan (edit --body) en deblokkeer, of splits de kaart",
    "kaart": "wacht op een kaart: trek die kaart vlot (waarom staat hij stil?); daarna gaat deze vanzelf verder",
    "toegang": "toegang/omgeving: zet het als bestand in de workflow-inbox en blokkeer met 'Wacht op "
               "workflow-inboxpunt <bestandsnaam>'",
    "overig": "deblokkeer, of zet de echte reden ('Wacht op <kaart-id> …', of een vraag in het vraagformat)",
}
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


def pinned_projects() -> set:
    """Project ids whose model the fork pins itself (config.yaml kanban.project_models, fork patch 10)."""
    try:
        import yaml
        cfg = yaml.safe_load((HOME / "config.yaml").read_text()) or {}
        return set(((cfg.get("kanban") or {}).get("project_models") or {}).keys())
    except Exception:  # noqa: BLE001
        return set()


PINNED = pinned_projects()


def project_model(slug, role=None):
    """The fixed model of a project (flow config projecten[].model: rollen.<role> or standaard), or None."""
    for p in fc.get("projecten") or []:
        if p.get("slug") == slug and isinstance(p.get("model"), dict):
            m = (p["model"].get("rollen") or {}).get(role) or p["model"].get("standaard") or {}
            return m.get("model") or None
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


# Een expliciete pauze door de eigenaar (notitie "Pauze op verzoek …" of "PAUZE: …" in het projectbestand) gaat vóór
# een eerder fasenplan-akkoord: dan nooit oproepen tot ACTIEF (inbox 20261002-0732).
EXPLICIT_PAUSE = re.compile(r"(?im)^\s*(?:<!--\s*)?(?:pauze\s+op\s+verzoek|pauze\s*:)")


def project_checks(projects, open_cards, intake_done, work=None, capacity=None, supply=None):
    """Project-level anomalies (id "project:<slug>"), over the open cards of all boards."""
    found = []
    for slug, p in projects.items():
        cards = open_cards.get(slug, [])
        title = f"Project {p['name']} ({p['status']})"
        s = (supply or {}).get(slug) or {}
        if p["status"] == "ACTIEF" and s.get("n", 0) < SUPPLY_MIN and s.get("plan") and not s.get("busy"):
            found.append({"id": f"project:{slug}", "title": title, "project": slug, "kind": "voorraad-laag",
                          "why": f"nog {s.get('n', 0)} coderkaart(en) klaar of startbaar (minimaal {SUPPLY_MIN}); plan nu "
                                 f"de eerstvolgende {SUPPLY_MIN} bouwkaarten (met volledige kleurplaat, zonder de -00 als "
                                 "voorganger); niet wachten tot de hele fase gepland is"})
        if p["status"] == "GEPAUZEERD" and ("f0-done", slug) in intake_done and not EXPLICIT_PAUSE.search(
                p["file"].read_text(encoding="utf-8", errors="replace")):
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


def appointment(reason, now):
    """True when the reason names a date (ISO or dd-mm[-yyyy]) whose day has not ended yet (NL time)."""
    today = datetime.fromtimestamp(now, NL)
    for m in DATE.finditer(reason):
        y, mo, d = (m.group(1), m.group(2), m.group(3)) if m.group(1) else (m.group(6) or today.year, m.group(5), m.group(4))
        try:
            day = datetime(int(y), int(mo), int(d), 23, 59, 59, tzinfo=NL)
        except ValueError:
            continue
        if day.timestamp() > now:
            return True
    return False


def moving(conn, ids, since):
    """True when one of the cards ``ids`` makes progress: running, or a run, event (no block) or comment (not the
    one a block appends) since ``since``."""
    for i in ids:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (i,)).fetchone()
        if (row and row["status"] == "running") or conn.execute(
                "SELECT 1 FROM task_runs WHERE task_id = ? AND (ended_at IS NULL OR started_at > ?) LIMIT 1",
                (i, since)).fetchone() or conn.execute(
                f"SELECT 1 FROM task_events WHERE task_id = ? AND created_at > ? AND kind NOT IN "
                f"({','.join('?' * len(NO_PROGRESS))}) LIMIT 1", (i, since, *NO_PROGRESS)).fetchone() or conn.execute(
                "SELECT 1 FROM task_comments c WHERE c.task_id = ? AND c.created_at > ? AND NOT EXISTS (SELECT 1 FROM "
                "task_events e WHERE e.task_id = c.task_id AND e.kind = 'blocked' AND ABS(e.created_at - c.created_at)"
                " <= 5) LIMIT 1", (i, since)).fetchone():
            return True
    return False


def cycles(blocked):
    """Groups of blocked cards whose reasons point at each other (via card ids): [[id, …], …]."""
    edges = {i: {j for j in CARD_ID.findall(b["reason"]) if j != i and j in blocked} for i, b in blocked.items()}

    def reach(start):
        seen, todo = set(), list(edges[start])
        while todo:
            n = todo.pop()
            if n not in seen:
                seen.add(n)
                todo += edges[n]
        return seen
    found = []
    for i in sorted(blocked):
        r = reach(i)
        group = sorted({i} | {j for j in r if i in reach(j)}) if i in r else []
        if len(group) > 1 and group not in found:
            found.append(group)
    return found


def first_in_line(group, blocked):
    """The card of a cycle that goes first: reviewer before security (TEAM.md "Reviewregels"), else the oldest."""
    for role in ("reviewer", "security"):
        own = [i for i in group if blocked[i]["assignee"] == role]
        if own:
            return min(own, key=lambda i: blocked[i]["created_at"])
    return min(group, key=lambda i: blocked[i]["created_at"])


def block_rule(conn, blocked, specific, waits, now, add):
    """R2 (one rule for every block) + the wachtkring and the handled inbox item (R3), for one board."""
    in_cycle = set()
    for group in cycles({i: b for i, b in blocked.items() if b["actief"]}):  # never wake a paused project
        first = first_in_line(group, blocked)
        in_cycle |= set(group)
        why = "reviewer gaat vóór security" if blocked[first]["assignee"] in ("reviewer", "security") else "oudste kaart"
        note = (f"Bordbewaking: wachtkring {' ↔ '.join(group)} doorbroken; deze kaart gaat eerst ({why}). "
                "Volgorde: de reviewer keurt goed op de exacte SHA, daarna pas security (TEAM.md, 'Reviewregels').")
        unblocks.append((first, note))
        add(blocked[first], "wachtkring", f"kring {' ↔ '.join(group)} (redenen verwijzen naar elkaar): {first} is "
            f"gedeblokkeerd ({why}); controleer of de volgorde nu klopt en zet de andere kaart(en) goed")
    for i, b in blocked.items():
        reason, r = b["reason"], b["reason"].lower()
        if i in in_cycle or i in waits or not b["actief"] or r.startswith(PREFIX):
            continue  # cycle handled; workflow-waits: #meldingen part; owner question: already in #vragen
        item = INBOX_ITEM.search(reason)
        if item and (INBOX / "afgehandeld" / f"{item.group(1)}.md").exists() \
                and not (INBOX / f"{item.group(1)}.md").exists():
            unblocks.append((i, f"Bordbewaking: workflow-inboxpunt {item.group(1)} is afgehandeld; de kaart gaat verder."))
            continue
        if item and (INBOX / f"{item.group(1)}.md").exists():
            continue  # the workflow side has the item open: that is progress
        age = now - b["since"]
        if appointment(reason, now):
            continue  # waits on an agreed date that has not passed yet (owner 03-10)
        if age < BLOCK_WAKE or (age < BLOCK_ASK and i in specific):
            continue  # a kleurplaat/needs_input block already woke the manager at once
        refs = [j for j in CARD_ID.findall(reason) if j != i]
        since = max(now - 3600, b["since"] + 60) if not refs else now - 3600
        if moving(conn, refs or [i], since):
            continue
        sort = ("kleurplaat" if r.startswith("kleurplaat onduidelijk") else "kaart" if refs
                else "toegang" if ACCESS.search(reason) else "overig")
        owner = "manager"
        if refs:
            row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (refs[0],)).fetchone()
            owner = f"{(row and row['assignee']) or 'zonder eigenaar'} ({refs[0]})"
        head = f"staat {int(age // 3600)} u {int(age % 3600 // 60)} min stil zonder voortgang (wacht op: {owner}): '{reason[:120]}'"
        if age >= BLOCK_ASK:
            ask = ("dit is workflowwerk, geen vraag voor #vragen: zet het in de workflow-inbox (als dat nog niet "
                   "gebeurd is) en blokkeer met 'Wacht op workflow-inboxpunt <bestandsnaam>'" if sort == "toegang" else
                   f"zet nu op deze kaart een vraag voor {OWNER} in het vraagformat (TEAM.md, eerst --check), met wat "
                   "al geprobeerd is")
            add(b, "blokkade-vraag", f"{head}. {ask}")
        else:
            add(b, "blokkade", f"{head}. Draaiboek: {PLAYBOOK[sort]}")


def anomalies(now):
    projects = active_projects(("ACTIEF", "GEPAUZEERD"))
    found = []
    global holds, bumps, unblocks, busy
    holds, bumps, unblocks = [], [], []
    open_cards, intake_done, work, supply = {}, set(), {}, {}
    running = {"total": 0, "coder": 0, "last_end": 0}
    waits = workflow_waits()
    for board, db in boards():
        conn = kanban(db)
        blocked, specific = {}, set()
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

            def add(kind, why, card=None):
                c = card or {"id": t["id"], "title": title, "project": slug or "-"}
                found.append({"id": c["id"], "title": c["title"], "project": c["project"], "kind": kind, "why": why})
                if kind in SPECIFIC_BLOCKS:
                    specific.add(c["id"])

            if workflow:
                add("workflow-kaart", "workflowpunten staan niet op het bord: zet dit als bestand in "
                    "~/.hermes/workflow-inbox/ (zie LEESMIJ.md) en archiveer de kaart")
                continue
            if not t["project_id"]:
                add("zonder-project", "open kaart zonder project")
            if not slug:
                continue
            open_cards.setdefault(slug, []).append(title)
            s = supply.setdefault(slug, {"n": 0, "plan": False, "busy": False})
            if t["assignee"] == BUILDER and (t["status"] == "ready" or t["status"] == "todo" and not conn.execute(
                    "SELECT 1 FROM task_links l JOIN tasks p ON p.id = l.parent_id WHERE l.child_id = ? AND p.status "
                    "NOT IN ('done', 'archived')", (t["id"],)).fetchone()):
                s["n"] += 1
            s["plan"] |= bool(PLAN_CODE.match(title)) or (t["assignee"] == MANAGER and t["status"] == "todo"
                                                         and not REVIEWISH.match(title) and not DECISION.match(title))
            s["busy"] |= t["assignee"] == MANAGER and t["status"] == "running"
            if t["status"] in ("ready", "running", "review"):
                work[slug] = True
            if (t["status"] in ("ready", "review") and (t["priority"] or 0) < REVIEW_PRIORITY
                    and (t["assignee"] in CHECKERS or t["status"] == "review"
                         or REVIEWISH.match(title))):
                moved = conn.execute("SELECT MAX(created_at) FROM task_events WHERE task_id = ? AND kind NOT IN "
                                     "('commented', 'heartbeat')", (t["id"],)).fetchone()[0] or t["created_at"]
                if now - moved > REVIEW_WAIT:
                    bumps.append(t["id"])
            fixed = None if t["project_id"] in PINNED else project_model(slug, t["assignee"])  # pinned by the fork: no card check
            if fixed and t["model_override"] != fixed:  # project with a fixed model (flow config projecten[].model)
                add("model-override", f"project {slug} heeft een vast model '{fixed}' (besluit {fc.owner_name()}), deze kaart heeft "
                    f"'{t['model_override'] or 'geen override'}'; zet het vaste model (hermes kanban --board {BOARD} set-model "
                    "<id> <model> --provider <provider> uit het projectbestand); geen ander model, ook geen escalatie")
            elif not fixed and t["project_id"] not in PINNED and t["model_override"] \
                    and t["model_override"] not in ALLOWED_OVERRIDES:  # only the escalation model (pinned: the fork decides)
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
                    if not UNAVAILABLE.search(t["last_failure_error"] or ""):  # a storing: reported once in #meldingen
                        add("mislukt-2x", "twee keer mislukt (geen storing): pas de escalatieregel toe (kleurplaat "
                            "herschrijven of overnemen), fout: " + (t["last_failure_error"] or "?")[:160])
                    continue
                blocked[t["id"]] = {"id": t["id"], "title": title, "project": slug, "reason": reason, "since": since,
                                    "assignee": t["assignee"], "created_at": t["created_at"],
                                    "actief": projects[slug]["status"] == "ACTIEF"}
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
        block_rule(conn, blocked, specific, waits, now, lambda card, kind, why: found.append(
            {"id": card["id"], "title": card["title"], "project": card["project"], "kind": kind, "why": why}))
        found += decision_checks(conn, projects, intake_done, now)
    capacity = {"free": running["total"] < HOST_CAP and running["coder"] < CODER_CAP
                and now - running["last_end"] >= IDLE_AFTER}
    busy = {slug for slug, s in supply.items() if s["busy"]}  # a manager card of that project runs: no wake-up
    return found + project_checks(projects, open_cards, intake_done, work, capacity, supply) + old_messages()


def old_messages():
    """Niet-opgeloste #meldingen-melding ouder dan 14 dagen (discord-cleanup.py zet "melding-oud:<id>"): één keer
    naar de manager; na de wekker (guard:<id>:melding-oud woken) niet meer."""
    out = []
    for key, info in dp.events_matching("melding-oud:").items():
        mid = f"melding-{key.split(':', 1)[1]}"
        if (dp.event_get(f"guard:{mid}:melding-oud") or {}).get("woken"):
            continue
        out.append({"id": mid, "title": info.get("tekst", "")[:80], "project": "-", "kind": "melding-oud",
                    "why": f"melding in #meldingen sinds {info.get('sinds', '?')} niet opgelost"
                           + (f" (kaart {info['card']})" if info.get("card") else "")
                           + "; los het op of zet een workflowpunt in de inbox. De melding blijft staan."})
    return out


# --- #meldingen (was kanban-stuck-check.py): time signals and failed runs, one message per episode ---
TRIAGE_SECONDS = int(D["triage_min"] * 60)
TODO_SECONDS = int(D["todo_min"] * 60)
READY_SECONDS = int(D["ready_uur"] * 3600)
SLOT_FREE_SECONDS = 10 * 60  # no run ended this recently: a free slot is not just a dispatcher tick behind
PROFILE_CAP = dict(fc.get("rollen.max_per_rol") or {})  # mirrors the dispatcher (1 per profile unless listed)
FAIL_KINDS = {"crashed": "gecrasht", "timed_out": "time-out", "gave_up": "opgegeven na herhaalde fouten"}
FAIL_WINDOW = 24 * 3600


def _last_move(conn, t):
    row = conn.execute("SELECT MAX(created_at) FROM task_events WHERE task_id = ? AND kind != 'commented'",
                       (t["id"],)).fetchone()
    return row[0] or t["created_at"]


def workflow_waits():
    path = fc.path("workflow_waits")
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}


def _unit(name, props):
    out = subprocess.run(["systemctl", "--user", "show", name, "-p", ",".join(props)],
                         capture_output=True, text=True, timeout=30, check=True).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def workflow_verdict(unit):
    """None while the workflow run is still going (skip the card), else ``(kind, text)`` to report."""
    try:
        svc = _unit(f"{unit}.service", ("ActiveState", "SubState", "Result", "ExecMainStatus"))
        tmr = _unit(f"{unit}.timer", ("ActiveState",))
    except (OSError, subprocess.SubprocessError, ValueError):
        return "onbekend", f"status van workflowrun {unit} onleesbaar"
    if svc.get("SubState") == "auto-restart":
        return None  # systemd restarts it itself
    if svc.get("Result", "success") != "success":
        return "mislukt", f"workflowrun {unit} mislukt ({svc.get('Result')}, exit {svc.get('ExecMainStatus')})"
    if tmr.get("ActiveState") == "active" or svc.get("ActiveState") in ("active", "activating", "reloading"):
        return None
    return "klaar", f"workflowrun {unit} klaar (exit {svc.get('ExecMainStatus')}), kaart staat nog blocked"


def slots(conns):
    """Host-wide running workers: ``{"total", "per", "last_end", "cap"}`` over every board DB."""
    per, last_end = {}, 0
    for conn in conns:
        for r in conn.execute("SELECT assignee, COUNT(*) FROM tasks WHERE status = 'running' GROUP BY assignee"):
            per[r[0]] = per.get(r[0], 0) + r[1]
        last_end = max(last_end, conn.execute("SELECT MAX(ended_at) FROM task_runs").fetchone()[0] or 0)
    try:
        m = re.search(r"^kanban:\n(?:[ \t]+.*\n)*?[ \t]+max_in_progress:\s*(\d+)\s*$",
                      (HOME / "config.yaml").read_text(encoding="utf-8"), re.M)
        cap = int(m.group(1)) if m else HOST_CAP
    except OSError:
        cap = HOST_CAP
    return {"total": sum(per.values()), "per": per, "last_end": last_end, "cap": cap}


def stuck_cards(now):
    """Cards that stand still on time signals (ACTIEF projects): [{id, title, project, why, since, episode, quiet}]."""
    projects = active_projects()
    conns = [(board, kanban(db)) for board, db in boards()]
    legacy = HOME / "kanban.db"
    host = slots([c for _b, c in conns] + ([kanban(legacy)] if legacy.exists() else []))
    waits, found = workflow_waits(), []
    for board, conn in conns:
        for t in conn.execute("SELECT id, title, status, assignee, project_id, workspace_path, created_at FROM tasks "
                              "WHERE status IN ('blocked', 'triage', 'todo', 'ready')"):
            slug = project_of(t, projects)
            if not slug or (t["title"] or "").lower().startswith("workflow:"):
                continue
            quiet, since = False, _last_move(conn, t)
            if t["status"] == "triage" and now - since > TRIAGE_SECONDS:
                why = "staat in triage; niemand pakt dit vanzelf op"
            elif t["status"] == "todo":
                parents = conn.execute("SELECT p.status, p.completed_at FROM task_links l JOIN tasks p ON "
                                       "p.id = l.parent_id WHERE l.child_id = ?", (t["id"],)).fetchall()
                if any(p["status"] not in ("done", "archived") for p in parents):
                    continue  # waits on an open parent: normal
                since = max([since] + [p["completed_at"] or 0 for p in parents])
                if now - since <= TODO_SECONDS:
                    continue
                why = "todo terwijl alle voorgangers klaar zijn; niet gestart"
            elif t["status"] == "ready" and t["assignee"] and now - since > READY_SECONDS:
                # slot busy: quiet, but keep the episode so a slot that frees up later does not report it again
                quiet = not (host["total"] < host["cap"] and host["per"].get(t["assignee"], 0)
                             < PROFILE_CAP.get(t["assignee"], 1) and now - host["last_end"] > SLOT_FREE_SECONDS)
                why = f"ready terwijl er een plek vrij is ({t['assignee']})"
            elif t["status"] == "blocked" and t["id"] in waits:
                verdict = workflow_verdict(waits[t["id"]].get("unit", ""))
                if verdict is None:
                    continue  # waits on a scheduled workflow run: not stuck
                kind, why = verdict
                since = conn.execute("SELECT MAX(created_at) FROM task_events WHERE task_id = ? AND kind = 'blocked'",
                                     (t["id"],)).fetchone()[0] or t["created_at"]
                found.append({"id": t["id"], "title": t["title"], "project": slug, "why": why, "since": int(since),
                              "episode": f"{board}:{t['id']}:workflow:{waits[t['id']].get('unit')}:{kind}"})
                continue
            else:
                continue  # blocks: the block rule (manager); running too long: te-lang-running
            found.append({"id": t["id"], "title": t["title"], "project": slug, "why": why, "since": int(since),
                          "episode": f"{board}:{t['id']}:{t['status']}:{int(since)}", "quiet": quiet})
    return found


def message(card):
    why = card["why"] if len(card["why"]) <= 200 else card["why"][:197] + "..."
    return (f"{fc.text('vastgelopen.vastgelopen')}: {card['id']} {card['title']} ({card['project']}) — {why}, "
            f"sinds {fc.tijd(card['since'])}")


def event_key(card):
    return f"{card['id']}:vastgelopen:" + card["episode"].split(":", 2)[2]


def failed_cards(now):
    """Runs that crashed, timed out or gave up in the last 24 h (ACTIEF projects): one message per event."""
    projects, found = active_projects(), []
    for _board, db in boards():
        for e in kanban(db).execute(
                "SELECT e.id, e.kind, e.task_id, t.title, t.project_id, t.workspace_path, t.last_failure_error, "
                "t.assignee FROM task_events e JOIN tasks t ON t.id = e.task_id "
                f"WHERE e.kind IN ({','.join('?' * len(FAIL_KINDS))}) AND e.created_at > ?", (*FAIL_KINDS, now - FAIL_WINDOW)):
            slug = project_of(e, projects)
            if slug:
                why = FAIL_KINDS[e["kind"]] + (f": {e['last_failure_error'][:150]}" if e["last_failure_error"] else "")
                found.append({"id": e["task_id"], "title": e["title"], "project": slug, "why": why,
                              "key": f"{e['task_id']}:mislukt:{e['id']}",
                              "unavailable": e["assignee"] == BUILDER and bool(UNAVAILABLE.search(e["last_failure_error"] or ""))})
    return found


def report(now, dry):
    """#meldingen (ping): stuck episodes, failed runs, builder unavailable; never twice (event register)."""
    send = (lambda text: print("zou melden (#meldingen): " + text)) if dry else \
        (lambda text: dp.send(dp.channels()["meldingen"], text, ping=True))
    for card in stuck_cards(now):
        key = event_key(card)
        if not card.get("quiet") and not dp.event_seen(key):
            msg = send(message(card))
            if not dry:
                dp.event_mark(key, channel=msg["channel_id"], message=msg["id"], episode=card["episode"],
                              text=msg["content"], card=card["id"])
    fails = [f for f in failed_cards(now) if not dp.event_seen(f["key"])]
    down = [f for f in fails if f["unavailable"]]
    key = f"bouwer-onbeschikbaar:{int(now // (6 * 3600))}"
    if down and not dp.event_seen(key):  # builder unavailable: ONE message per 6-hour window, never another model
        msg = send(fc.text("vastgelopen.bouwer_onbeschikbaar", aantal=len(down),
                           kaarten=", ".join(sorted({f["id"] for f in down})[:5])))
        if not dry:
            dp.event_mark(key, channel=msg["channel_id"], message=msg["id"], text=msg["content"])
    for f in fails:
        if f["unavailable"]:
            if not dry:
                dp.event_mark(f["key"], grouped=key)
            continue
        msg = send(fc.text("vastgelopen.mislukt", id=f["id"], titel=f["title"], project=f["project"], waarom=f["why"]))
        if not dry:
            dp.event_mark(f["key"], channel=msg["channel_id"], message=msg["id"], text=msg["content"], card=f["id"])


def wake_manager(items):
    lines = "\n".join(f"- {a['id']} [{a['project']}] {a['title'][:80]}: {a['kind']} — {a['why']}" for a in items)
    prompt = ("Bordbewaking (automatisch, van de workflowkant). Deze kaarten wijken af; los ze op volgens je SOUL "
              "('Bordbewaking'). Je zit in een CLI-sessie: post niets in Discord, behalve een vraag voor "
              f"{OWNER} via een kaartblokkade in het vraagformat. Een workflowfout zet je als bestand in "
              "~/.hermes/workflow-inbox/ (LEESMIJ.md), nooit als kaart of melding. Blokkades, een wachtkring en een te "
              "lage codervoorraad los je eerst op, vóór je eigen planwerk.\n" + lines)
    r = subprocess.run([HERMES, "chat", "-Q", "-q", prompt], capture_output=True, text=True, timeout=1800,
                       env={"HOME": str(Path.home()), "PATH": f"{Path.home()}/.local/bin:/usr/bin:/bin"})
    return r.returncode, (r.stdout or "")[-1500:]


def kanban_cmd(*args):
    return subprocess.run([HERMES, "kanban", "--board", BOARD, *args], capture_output=True, text=True, timeout=120,
                          env={"HOME": str(Path.home()), "PATH": f"{Path.home()}/.local/bin:/usr/bin:/bin"}).returncode


def due(a, now):
    """Woken again for the same card and kind: blocks, cycle and supply every hour, the rest every 6 hours."""
    info = dp.event_get(f"guard:{a['id']}:{a['kind']}") or {}
    return now - float(info.get("woken", 0)) >= (MANAGER_REWAKE if a["kind"] in MANAGER_KINDS else REPEAT)


def bundles(found, now, running=()):
    """{project: [items, most urgent first]}: ONE wake-up per project per round (owner 03-10: the manager is the most
    expensive role). The per-item rules (``due``) decide what is in it; a project is woken at most once per
    BUNDLE_GAP, unless a direct item (DIRECT) is new since its last wake-up; never while a manager card of the
    project runs (``running``; the items stay for the next round)."""
    out = {}
    for a in found:
        if due(a, now):
            out.setdefault(a["project"], []).append(a)
    for slug in list(out):
        last = float((dp.event_get(f"guard-project:{slug}") or {}).get("woken", 0))
        new_direct = any(a["kind"] in DIRECT and not (dp.event_get(f"guard:{a['id']}:{a['kind']}") or {}).get("woken")
                         for a in out[slug])
        if slug in running or (now - last < BUNDLE_GAP and not new_direct):
            del out[slug]
        else:
            out[slug].sort(key=lambda a: URGENCY.index(a["kind"]) if a["kind"] in URGENCY else len(URGENCY))
    return out


def log(line):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(f"{datetime.now(NL).isoformat(timespec='seconds')} {line}\n")


def main():
    now = time.time()
    found = anomalies(now)
    if "--dry-run" in sys.argv:  # read-only: no lock, so it never waits on a running guard
        for label, items in (("zou tegenhouden", [t for t, _r in holds]), ("zou voorrang geven (prioriteit 10)", bumps),
                             ("zou deblokkeren", [t for t, _n in unblocks])):
            if items:
                print(f"{label}: " + ", ".join(items))
        report(now, dry=True)
        for a in found:
            print(f"{a['id']} [{a['project']}] {a['kind']}: {a['why']} | {a['title'][:70]}")
        print(f"{len(found)} afwijking(en)")
        woken = bundles(found, now, busy)
        for slug in sorted({a["project"] for a in found}):
            items = woken.get(slug)
            print(f"wekker [{slug}]: " + (f"1 ({', '.join(a['id'] + ':' + a['kind'] for a in items)})" if items else
                                          "geen" + (" (managerrun loopt)" if slug in busy else " (nog niet aan de beurt)")))
        return
    _lock = dp.single_instance("board-guard")  # noqa: F841 (held until exit)
    for task_id in bumps:  # review/herreview/end-check waiting > 15 min: before new build cards
        log(f"voorrang {task_id}: prioriteit {REVIEW_PRIORITY} (exit {kanban_cmd('edit', task_id, '--priority', str(REVIEW_PRIORITY))})")
    for task_id, reason in holds:  # hold back build cards with an incomplete kleurplaat before they start
        log(f"tegengehouden {task_id}: {reason} (exit {kanban_cmd('block', task_id, reason)})")
    for task_id, note in unblocks:  # wachtkring broken / workflow-inbox item handled
        log(f"gedeblokkeerd {task_id}: {note} (exit {kanban_cmd('unblock', '--reason', note, task_id)})")
    report(now, dry=False)
    for slug, items in bundles(found, now, busy).items():
        for a in items:
            dp.event_mark(f"guard:{a['id']}:{a['kind']}", woken=now, card=a["id"])
        dp.event_mark(f"guard-project:{slug}", woken=now)
        log(f"manager gewekt [{slug}]: " + "; ".join(f"{a['id']}:{a['kind']}" for a in items))
        code, out = wake_manager(items)
        log(f"manager klaar [{slug}] (exit {code}): {out.strip().splitlines()[-1][:300] if out.strip() else '-'}")


if __name__ == "__main__":
    main()
