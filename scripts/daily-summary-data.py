#!/usr/bin/env python3
"""Data for the 08:00 daily summary (cron --script): the Kanban cards of active projects only,
grouped as done yesterday / in progress / stuck. Output is injected into the agent prompt."""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flow_config as fc  # noqa: E402
from team_projects import active_projects, boards, kanban, project_of  # noqa: E402

projects = active_projects()
today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
start, end = (today - timedelta(days=1)).timestamp(), today.timestamp()
groups = {"AF (gisteren)": [], "BEZIG": [], "VAST": []}
rows = [(conn, t) for _board, db in boards() for conn in [kanban(db)] for t in conn.execute(
    "SELECT id, title, status, assignee, project_id, workspace_path, completed_at "
    "FROM tasks WHERE status NOT IN ('archived')")]


def reason(conn, task_id):
    """The live block reason (wait state), so the summary never repeats an old plan."""
    ev = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked' ORDER BY id DESC LIMIT 1",
                      (task_id,)).fetchone()
    try:
        text = (json.loads(ev["payload"]) or {}).get("reason", "") if ev and ev["payload"] else ""
    except (ValueError, TypeError):
        text = ""
    return " ".join((text or "").split())[:200] or "geen reden"


for conn, t in rows:
    if (t["title"] or "").lower().startswith("workflow:"):
        continue  # workflow items are not project work (owner rule 30-09: #samenvatting is for projects only)
    slug = project_of(t, projects)
    if not slug:
        continue
    line = f"- {t['id']} [{slug}] {t['title']} ({t['assignee']})"
    if t["status"] == "done" and t["completed_at"] and start <= t["completed_at"] < end:
        groups["AF (gisteren)"].append(line)
    elif t["status"] in ("running", "review", "ready"):
        groups["BEZIG"].append(line + f" — {t['status']}")
    elif t["status"] == "blocked":
        groups["VAST"].append(line + f" — reden nu: {reason(conn, t['id'])}")
print("Borden:", ", ".join(b for b, _ in boards()) or "geen")
print("Actieve projecten:", ", ".join(sorted(projects)) or "geen")
if not any(groups.values()):
    print("GEEN ACTIEVE KAARTEN")
for name, lines in groups.items():
    print(f"\n{name} ({len(lines)}):")
    print("\n".join(lines) if lines else "- geen")
meting = []
for board, db in boards():
    conn = kanban(db)
    for t in conn.execute("SELECT id, title, created_at, completed_at, assignee, project_id, workspace_path FROM tasks "
                          "WHERE status = 'done' AND completed_at >= ? AND completed_at < ?", (start, end)):
        slug = project_of(t, projects)
        if not slug or t["assignee"] != fc.get("rollen.bouwer"):
            continue
        ev = {k: n for k, n in conn.execute("SELECT kind, COUNT(*) FROM task_events WHERE task_id = ? "
                                            "AND kind IN ('review_requested', 'changes_requested') GROUP BY kind", (t["id"],))}
        runs = conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (t["id"],)).fetchone()[0]
        meting.append(f"- {t['id']} [{slug}] runs {runs}, reviewrondes {ev.get('review_requested', 0)}, "
                      f"afgekeurd {ev.get('changes_requested', 0)}, doorlooptijd {(t['completed_at'] - t['created_at']) / 3600:.1f} u")
print(f"\nMETING per bouwkaart, gisteren af ({len(meting)}):")
print("\n".join(meting) if meting else "- geen")
