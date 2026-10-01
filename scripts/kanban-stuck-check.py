#!/usr/bin/env python3
"""Stuck check (cron, --no-agent, every 5 min): time signals only, one Discord message per episode.
Reported: a card blocked for more than 6 hours for a reason nobody else watches (e.g. a tool or access
problem); a block whose reason starts with "beslissing manager" at once. NOT reported here (board-guard.py
wakes the manager for these, and escalates after 6 h; audit S2): blocks that wait on the owner (needs_input,
the question prefix, "Beslissing: …"), "Wacht op …" wait states, blocks without a reason, "kleurplaat
onduidelijk", ready cards without an assignee, and runs that take too long. Also reported (the dispatcher
never moves these on by itself):
- triage for more than 1 hour (kanban.auto_decompose is off, so nobody picks triage up);
- todo with every parent done/archived and still not started 30 minutes after the last one finished
  (a todo that waits on an open parent is normal and never reported);
- ready for more than 2 hours while a worker slot for its assignee has been free for 10+ minutes.
"Since" for triage/todo/ready = the card's last non-comment event, so any real status change restarts it.
A blocked card listed in the workflow-waits file (flow config paden.workflow_waits; {"<task_id>": {"unit": "<systemd --user
unit>"}}, written by the workflow side together with a "Wacht op workflowkant:" comment) is skipped
while that unit's timer or service is still going; it is reported at once when the run failed, or
when it finished (timer off) and the card is still blocked. An unreadable status is reported too.
Messages go to #meldingen with a ping for the owner (discord_post.py). Runs that crashed, timed out or gave up (last 24 h) are reported at once as "❌ Kaart mislukt". A new
"beslissing manager" also wakes the manager in a CLI session (no Discord output). Every message is recorded
in the shared event register (discord-events.json, key "<kaart>:vastgelopen:<status>:<sinds>" or
"<kaart>:mislukt:<event>"), so it is never posted twice; discord-cleanup.py marks it "✅ opgelost" later.
Workflow items are not on the board (they go to ~/.hermes/workflow-inbox/; owner rule 30-09), so a
"workflow: …" card is skipped here; board-guard.py asks the manager to move it. Only cards of active projects.
Silent when nothing is stuck; each episode is reported once
(state in ~/.hermes/state/).

The first run (no state file) records the current stuck cards silently as a baseline.
``--test <task_id>`` sends one message marked [test] for that card and records nothing.
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
STUCK_SECONDS = int(D["vastgelopen_uur"] * 3600)
TRIAGE_SECONDS = int(D["triage_min"] * 60)
TODO_SECONDS = int(D["todo_min"] * 60)
READY_SECONDS = int(D["ready_uur"] * 3600)
SLOT_FREE_SECONDS = 10 * 60  # no run ended this recently: a free slot is not just a dispatcher tick behind
# Mirrors the dispatcher: kanban.max_in_progress (host-wide) and, with max_in_progress_per_profile
# unset, 1 worker per profile except the ones in rollen.max_per_rol (flow config).
DEFAULT_HOST_CAP = int(fc.get("rollen.max_totaal"))
PROFILE_CAP = dict(fc.get("rollen.max_per_rol") or {})
BUILDER = fc.get("rollen.bouwer")
PREFIX = fc.question_prefix().lower().rstrip(":")
MANAGER_DECISION = "beslissing manager"
STATE = HOME / "state" / "stuck-notified.json"
WAITS = fc.path("workflow_waits")


def _since(conn, task_id, kinds):
    row = conn.execute(
        f"SELECT MAX(created_at) FROM task_events WHERE task_id = ? AND kind IN ({','.join('?' * len(kinds))})",
        (task_id, *kinds)).fetchone()
    return row[0]


def _last_move(conn, t):
    row = conn.execute("SELECT MAX(created_at) FROM task_events WHERE task_id = ? AND kind != 'commented'",
                       (t["id"],)).fetchone()
    return row[0] or t["created_at"]


def workflow_waits():
    try:
        return json.loads(WAITS.read_text(encoding="utf-8")) if WAITS.exists() else {}
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


def host_cap():
    try:
        m = re.search(r"^kanban:\n(?:[ \t]+.*\n)*?[ \t]+max_in_progress:\s*(\d+)\s*$",
                      (HOME / "config.yaml").read_text(encoding="utf-8"), re.M)
        return int(m.group(1)) if m else DEFAULT_HOST_CAP
    except OSError:
        return DEFAULT_HOST_CAP


def slots(conns):
    """Host-wide running workers: ``{"total", "per", "last_end"}`` over every board DB."""
    per, last_end = {}, 0
    for conn in conns:
        for r in conn.execute("SELECT assignee, COUNT(*) FROM tasks WHERE status = 'running' GROUP BY assignee"):
            per[r[0]] = per.get(r[0], 0) + r[1]
        last_end = max(last_end, conn.execute("SELECT MAX(ended_at) FROM task_runs").fetchone()[0] or 0)
    return {"total": sum(per.values()), "per": per, "last_end": last_end}


def stuck_cards(now):
    projects = active_projects()
    conns = [(board, kanban(db)) for board, db in boards()]
    legacy = HOME / "kanban.db"
    host = slots([c for _b, c in conns] + ([kanban(legacy)] if legacy.exists() else []))
    host["cap"] = host_cap()
    found = []
    waits = workflow_waits()
    for board, conn in conns:
        found += _stuck_on_board(board, conn, projects, now, host, waits)
    return found


def _slot_free(host, assignee, now):
    return (host["total"] < host["cap"]
            and host["per"].get(assignee, 0) < PROFILE_CAP.get(assignee, 1)
            and now - host["last_end"] > SLOT_FREE_SECONDS)


def _stuck_on_board(board, conn, projects, now, host=None, waits=None):
    found = []
    for t in conn.execute("SELECT id, title, status, assignee, block_kind, project_id, workspace_path, created_at FROM tasks "
                          "WHERE status IN ('blocked', 'running', 'triage', 'todo', 'ready')"):
        slug = project_of(t, projects)
        if not slug or (t["title"] or "").lower().startswith("workflow:"):
            continue
        decision, due, quiet = False, False, False  # due: the lane check above already passed its own threshold
        if t["status"] == "triage":
            since = _last_move(conn, t)
            if now - since <= TRIAGE_SECONDS:
                continue
            why = "staat in triage; niemand pakt dit vanzelf op"
            due = True
        elif t["status"] == "todo":
            parents = conn.execute("SELECT p.status, p.completed_at FROM task_links l JOIN tasks p ON p.id = l.parent_id "
                                   "WHERE l.child_id = ?", (t["id"],)).fetchall()
            if any(p["status"] not in ("done", "archived") for p in parents):
                continue  # waits on an open parent: normal
            since = max([_last_move(conn, t)] + [p["completed_at"] or 0 for p in parents])
            if now - since <= TODO_SECONDS:
                continue
            why = "todo terwijl alle voorgangers klaar zijn; niet gestart"
            due = True
        elif t["status"] == "ready":
            if not t["assignee"]:
                continue  # board-guard: ready-zonder-eigenaar
            since = _last_move(conn, t)
            if now - since <= READY_SECONDS:
                continue
            # Slot busy: stay quiet but keep the episode, so a slot that frees up later
            # does not report the same card a second time.
            quiet = not host or not _slot_free(host, t["assignee"], now)
            why = f"ready terwijl er een plek vrij is ({t['assignee'] or 'geen assignee'})"
            due = True
        elif t["status"] == "blocked":
            since = _since(conn, t["id"], ("blocked",)) or t["created_at"]
            reason = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked' "
                                  "ORDER BY id DESC LIMIT 1", (t["id"],)).fetchone()
            try:
                reason = (json.loads(reason[0]) or {}).get("reason", "") if reason and reason[0] else ""
            except (ValueError, TypeError):
                reason = ""
            why = f"geblokkeerd: {reason}".strip() if reason else "geblokkeerd"
            r = reason.strip().lower()
            decision = r.startswith(MANAGER_DECISION)
            waits_on_owner = (t["block_kind"] == "needs_input" or r.startswith(PREFIX)
                              or (t["title"] or "").lower().startswith("beslissing"))
            if not decision and t["id"] not in (waits or {}) and (
                    waits_on_owner or not r or r.startswith("wacht op") or r.startswith("kleurplaat onduidelijk")):
                continue  # board-guard.py watches these (manager first, escalation after 6 h)
            wait = (waits or {}).get(t["id"])
            if wait and not decision:
                verdict = workflow_verdict(wait.get("unit", ""))
                if verdict is None:
                    continue  # waits on a scheduled workflow run: not stuck
                kind, text = verdict
                found.append({"id": t["id"], "title": t["title"], "project": slug, "why": text, "decision": False,
                              "since": int(since), "episode": f"{board}:{t['id']}:workflow:{wait.get('unit')}:{kind}"})
                continue
        else:
            continue  # running too long: board-guard.py (te-lang-running)
        if since and (due or decision or now - since > STUCK_SECONDS):
            found.append({"id": t["id"], "title": t["title"], "project": slug, "why": why, "decision": decision,
                          "since": int(since), "episode": f"{board}:{t['id']}:{t['status']}:{int(since)}", "quiet": quiet})
    return found


def message(card, test=False):
    stamp = datetime.fromtimestamp(card["since"]).astimezone().strftime("%d-%m %H:%M")
    why = card["why"] if len(card["why"]) <= 200 else card["why"][:197] + "..."
    label = fc.text("vastgelopen.beslissing_manager") if card.get("decision") else fc.text("vastgelopen.vastgelopen")
    return f"{'[test] ' if test else ''}{label}: {card['id']} {card['title']} ({card['project']}) — {why}, sinds {stamp}"


UNAVAILABLE = re.compile(r"(?i)quota|rate.?limit|\b429\b|\b503\b|overloaded|unavailable|capacity|usage limit|exhausted|"
                         r"insufficient|credits|no available|not available")
FAIL_KINDS = {"crashed": "gecrasht", "timed_out": "time-out", "gave_up": "opgegeven na herhaalde fouten"}
FAIL_WINDOW = 24 * 3600


def send(text):
    return dp.send(dp.channels()["meldingen"], text, ping=True)


def event_key(card):
    return f"{card['id']}:vastgelopen:" + card["episode"].split(":", 2)[2]


def failed_cards(now):
    """Runs that crashed, timed out or gave up in the last 24 h (active projects): one message per event."""
    projects, found = active_projects(), []
    for board, db in boards():
        conn = kanban(db)
        for e in conn.execute("SELECT e.id, e.kind, e.task_id, e.created_at, t.title, t.project_id, t.workspace_path, "
                              "t.last_failure_error, t.assignee FROM task_events e JOIN tasks t ON t.id = e.task_id "
                              f"WHERE e.kind IN ({','.join('?' * len(FAIL_KINDS))}) AND e.created_at > ?",
                              (*FAIL_KINDS, now - FAIL_WINDOW)):
            slug = project_of(e, projects)
            if slug:
                why = FAIL_KINDS[e["kind"]] + (f": {e['last_failure_error'][:150]}" if e["last_failure_error"] else "")
                found.append({"id": e["task_id"], "title": e["title"], "project": slug, "why": why,
                              "key": f"{e['task_id']}:mislukt:{e['id']}",
                              "unavailable": e["assignee"] == BUILDER and bool(UNAVAILABLE.search(e["last_failure_error"] or ""))})
    return found


def wake_manager(card):
    """"beslissing manager" is the manager's job: wake him in a CLI session (nothing is posted in Discord)."""
    prompt = (f"Automatische wekker van de workflowkant: kaart {card['id']} '{card['title']}' ({card['project']}) "
              f"staat op 'beslissing manager': {card['why']}. Handel dit af volgens je SOUL (Blokkade 'beslissing "
              "manager'). Je zit in een CLI-sessie: post niets in Discord. Is het een productkeuze, stel dan een "
              f"{fc.question_prefix().rstrip(':')} volgens TEAM.md.")
    subprocess.Popen([fc.hermes_bin(), "chat", "-Q", "-q", prompt],
                     env={"HOME": str(Path.home()), "PATH": f"{Path.home()}/.local/bin:/usr/bin:/bin"},
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def main():
    _lock = dp.single_instance("stuck-check")  # noqa: F841 (held until exit)
    now = time.time()
    if len(sys.argv) == 3 and sys.argv[1] == "--test":
        dbs = [db for _b, db in boards()] + [HOME / "kanban.db"]
        t = next((r for db in dbs for r in [kanban(db).execute(
            "SELECT id, title FROM tasks WHERE id = ?", (sys.argv[2],)).fetchone()] if r), None)
        if not t:
            sys.exit(f"onbekende kaart {sys.argv[2]}")
        send(message({"id": t["id"], "title": t["title"], "project": "test", "why": "testmelding",
                      "since": int(now)}, test=True))
        return
    cards = stuck_cards(now)
    if STATE.exists():  # migrate the old per-script state into the shared event register
        for ep in json.loads(STATE.read_text() or "[]"):
            key = f"{ep.split(':')[1]}:vastgelopen:" + ep.split(":", 2)[2]
            if not dp.event_seen(key):
                dp.event_mark(key, migrated=True, episode=ep)
        STATE.rename(STATE.with_suffix(".migrated.json"))
    for card in cards:
        key = event_key(card)
        if card.get("quiet") or dp.event_seen(key):
            continue
        msg = send(message(card))
        dp.event_mark(key, channel=msg["channel_id"], message=msg["id"], episode=card["episode"],
                      text=msg["content"], card=card["id"])
        if card.get("decision"):
            wake_manager(card)
    fails = [f for f in failed_cards(now) if not dp.event_seen(f["key"])]
    down = [f for f in fails if f.get("unavailable")]
    if down:  # builder unavailable (its models): the cards wait, ONE message per 6-hour window, never another model
        key = f"bouwer-onbeschikbaar:{int(now // (6 * 3600))}"
        if not dp.event_seen(key):
            msg = send(fc.text("vastgelopen.bouwer_onbeschikbaar", aantal=len(down),
                               kaarten=", ".join(sorted({f["id"] for f in down})[:5])))
            dp.event_mark(key, channel=msg["channel_id"], message=msg["id"], text=msg["content"])
        for f in down:
            dp.event_mark(f["key"], grouped=key)
    for fail in [f for f in fails if not f.get("unavailable")]:
        if dp.event_seen(fail["key"]):
            continue
        msg = send(fc.text("vastgelopen.mislukt", id=fail["id"], titel=fail["title"], project=fail["project"],
                           waarom=fail["why"]))
        dp.event_mark(fail["key"], channel=msg["channel_id"], message=msg["id"], text=msg["content"], card=fail["id"])


if __name__ == "__main__":
    main()
