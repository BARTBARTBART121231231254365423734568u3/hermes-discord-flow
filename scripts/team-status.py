#!/usr/bin/env python3
"""team-status.py [<project-slug>]: live status of the projects, read-only, the ONLY source for a status overview.

The manager runs this before every status answer (SOUL: "Status altijd live"). Everything comes from the
live board and systemd at this moment, never from earlier in a conversation:
- per project (STATUS ACTIEF/GEPAUZEERD, or the given slug): status line, open cards per state with the
  live reason of blocked cards (the wait state, e.g. "Wacht op …"), the running time and last sign of life
  of running cards, and the cards done in the last 24 hours;
- workflow runs a card waits on (team/workflow-waits.json): live unit state (running since …, or not),
  the next timer start, the progress (processed/total) when the entry names its files, and the last line
  of its start log;
- open "workflow: …" cards.
"""
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flow_config as fc  # noqa: E402
from team_projects import HOME, active_projects, boards, kanban, project_of  # noqa: E402

NL = fc.tz()
OWNER = fc.owner_name()
PREFIX = fc.question_prefix().lower().rstrip(":")
ORDER = ["running", "review", "ready", "todo", "triage", "blocked"]


def hhmm(ts, day=False):
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts, NL).strftime("%d-%m %H:%M" if day else "%H:%M")


def age(sec):
    sec = int(sec)
    return f"{sec // 3600} u {sec % 3600 // 60} min" if sec >= 3600 else f"{sec // 60} min"


def reason(conn, task_id):
    ev = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked' ORDER BY id DESC LIMIT 1",
                      (task_id,)).fetchone()
    try:
        text = (json.loads(ev["payload"]) or {}).get("reason", "") if ev and ev["payload"] else ""
    except (ValueError, TypeError):
        text = ""
    return " ".join((text or "").split())[:300] or "(geen reden)"


def nl_time(stamp):
    """systemd timestamp ("Wed 2026-09-30 21:00:00 UTC") → "30-09 23:00" Dutch time; unchanged when unparsable."""
    try:
        return datetime.strptime(stamp.strip(), "%a %Y-%m-%d %H:%M:%S %Z").replace(tzinfo=ZoneInfo("UTC")) \
            .astimezone(NL).strftime("%d-%m %H:%M")
    except ValueError:
        return stamp


def question_state(conn, task_id, blocked_reason):
    """For a card blocked on a question to the owner: 'open' or 'beantwoord, wordt verwerkt' (he clicked or typed an
    answer in the #vragen post, but the manager has not yet put it on the card). None when it is no question."""
    row = conn.execute("SELECT block_kind FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not (blocked_reason.lower().startswith(PREFIX) or (row and row[0] == "needs_input"
                                                                         and not blocked_reason.lower().startswith("wacht op"))):
        return None
    ev = conn.execute("SELECT created_at FROM task_events WHERE task_id = ? AND kind = 'blocked' ORDER BY id DESC LIMIT 1",
                      (task_id,)).fetchone()
    since = ev[0] if ev else 0
    try:
        thread = (json.loads((HOME / "state" / "discord-events.json").read_text()).get(f"{task_id}:post") or {}).get("thread")
    except (OSError, ValueError):
        thread = None
    if thread:
        try:
            s = sqlite3.connect(f"file:{HOME / 'state.db'}?mode=ro", uri=True, timeout=5)
            hit = s.execute("SELECT 1 FROM messages m JOIN sessions x ON x.id = m.session_id WHERE x.session_key LIKE ? "
                            "AND m.role = 'user' AND m.timestamp > ? LIMIT 1", (f"%:thread:{thread}:%", since)).fetchone()
            if hit:
                return "beantwoord, wordt verwerkt"
        except sqlite3.Error:
            pass
    return "open"


def systemctl(*args):
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def unit_line(card, w, now):
    unit = w.get("unit", "")
    props = dict(l.split("=", 1) for l in systemctl("show", f"{unit}.service", "-p",
                 "ActiveState,ActiveEnterTimestampMonotonic,ExecMainStartTimestamp,ExecMainExitTimestamp,Result")
                 .splitlines() if "=" in l)
    state = props.get("ActiveState", "?")
    if state in ("active", "activating"):
        text = f"{unit}: DRAAIT NU (gestart {nl_time(props.get('ExecMainStartTimestamp', '?'))})"
    else:
        text = f"{unit}: draait niet (laatste resultaat {props.get('Result', '?')}, " \
               f"gestopt {nl_time(props.get('ExecMainExitTimestamp') or '-')})"
    timer = systemctl("is-enabled", f"{unit}.timer").strip()
    nxt = systemctl("show", f"{unit}.timer", "-p", "NextElapseUSecRealtime").split("=", 1)[-1].strip()
    text += f"; timer {timer or '?'}" + (f", volgende geplande start {nl_time(nxt)} (geen effect zolang hij draait)" if nxt and timer == "enabled" else "")
    prog = w.get("progress") or {}
    try:
        total = sum(1 for l in Path(prog["total"]).read_text().splitlines() if l.strip())
        done = len(json.loads(Path(prog["checkpoint"]).read_text()).get(prog.get("key", "handled"), []))
        text += f"; voortgang {done} van {total} ({done * 100 // max(total, 1)}%)"
    except (KeyError, OSError, ValueError):
        pass
    if w.get("log"):
        try:
            last = [l for l in Path(w["log"]).expanduser().read_text().splitlines() if "GESTART" in l][-1:]
            if last:
                text += f"; startlog: {last[0][:160]}"
        except OSError:
            pass
    return f"  - wacht-run voor {card}: {text}"


def drain_line(now):
    """A planned drain (drain-restart.py) pauses the dispatcher on purpose: show it, it is not a fault."""
    try:
        info = json.loads((HOME / "state" / "drain.json").read_text())
    except (OSError, ValueError):
        return None
    at, until = float(info.get("at", 0)), float(info.get("until") or info.get("at", 0) + 7200)
    fmt = lambda ts: datetime.fromtimestamp(ts, NL).strftime("%H:%M")  # noqa: E731
    return fc.text("drain.status_regel", sinds=fmt(at), tot=fmt(until))


def main():
    now = time.time()
    only = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else None
    projects = active_projects(("ACTIEF", "GEPAUZEERD", "GESTOPT") if only else ("ACTIEF", "GEPAUZEERD"))
    if only:
        projects = {s: p for s, p in projects.items() if s == only}
        if not projects:
            sys.exit(f"onbekend project {only} (slug = bestandsnaam in team/projects)")
    try:
        waits = json.loads(fc.path("workflow_waits").read_text())
    except (OSError, ValueError):
        waits = {}
    per = {s: {"open": [], "done": [], "runs": []} for s in projects}
    workflow = []
    for _board, db in boards():
        conn = kanban(db)
        for t in conn.execute("SELECT id, title, status, assignee, project_id, workspace_path, completed_at, "
                              "last_heartbeat_at, current_run_id FROM tasks WHERE status != 'archived'"):
            title = t["title"] or ""
            if title.lower().startswith("workflow:") and t["status"] != "done":
                workflow.append(f"- {t['id']} {title} ({t['status']})")
                continue
            slug = project_of(t, projects)
            if not slug:
                continue
            if t["status"] == "done":
                if t["completed_at"] and now - t["completed_at"] < 24 * 3600:
                    per[slug]["done"].append((t["completed_at"], f"  - {t['id']} {title} (af {hhmm(t['completed_at'])})"))
                continue
            extra = ""
            if t["status"] == "blocked":
                r = reason(conn, t["id"])
                q = question_state(conn, t["id"], r)
                extra = (f" — VRAAG AAN {OWNER.upper()}: {q.upper()}" if q else "") + f" — reden nu: {r}"
                if t["id"] in waits:
                    per[slug]["runs"].append(unit_line(t["id"], waits[t["id"]], now))
            elif t["status"] == "running":
                run = conn.execute("SELECT started_at FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                                   (t["id"],)).fetchone()
                if run and run["started_at"]:
                    extra = f" — run loopt {age(now - run['started_at'])} (sinds {hhmm(run['started_at'])})"
                if t["last_heartbeat_at"]:
                    extra += f", laatste teken van leven {age(now - t['last_heartbeat_at'])} geleden"
            per[slug]["open"].append((ORDER.index(t["status"]) if t["status"] in ORDER else 9,
                                      f"  - {t['id']} {t['status']} ({t['assignee'] or 'geen eigenaar'}) {title}{extra}"))
    print(f"LIVE STATUS {datetime.fromtimestamp(now, NL).strftime('%d-%m-%Y %H:%M')} (Nederlandse tijd). "
          "Alleen dit is de stand; wat eerder in een gesprek stond, kan verouderd zijn.")
    drain = drain_line(now)
    if drain:
        print(drain)
    for slug, p in projects.items():
        d = per[slug]
        print(f"\n== {p['name']} ({slug}): STATUS {p['status']}")
        print(f"Open kaarten ({len(d['open'])}):")
        print("\n".join(line for _o, line in sorted(d["open"])) if d["open"] else "  - geen")
        if d["runs"]:
            print("Workflowruns (live uit systemd):")
            print("\n".join(d["runs"]))
        done = [line for _at, line in sorted(d["done"], reverse=True)]
        print(f"Afgerond in de laatste 24 uur ({len(done)}, nieuwste eerst{', de laatste 8' if len(done) > 8 else ''}):")
        print("\n".join(done[:8]) if done else "  - geen")
    titles = []
    for p in sorted(x for x in fc.path("workflow_inbox").glob("*.md") if x.name != "LEESMIJ.md"):
        first = next((l for l in p.read_text(encoding="utf-8", errors="replace").splitlines() if l.startswith("# ")), p.name)
        titles.append(f"- {p.name[:13]} {first[2:].strip()[:120]}")
    print(f"\nOpen workflowpunten (workflow-inbox; de workflowkant lost ze op) ({len(titles)}):")
    print("\n".join(titles) if titles else "- geen")
    if not only and workflow:
        print(f"\nWorkflow-kaarten op het bord (horen in de inbox) ({len(workflow)}):")
        print("\n".join(workflow))


if __name__ == "__main__":
    main()
