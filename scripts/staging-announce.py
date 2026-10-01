#!/usr/bin/env python3
"""Staging announcements (cron, --no-agent, every 2 min): every done card of an active project
whose review run recorded ``metadata.merged_commit`` (sdlc-review merges to staging, then
completes) is posted once, silently, to #staging as "[Project] ✅ <id> <titel> — <staging-URL>",
but only once that commit is on the project's remote staging branch. Cards completed without a
merge (e.g. a self-review) are never announced.

Phases/blocks: card codes F<fase>[<blok>]-<nr> (TEAM.md; old "2A-0"/"2-37" still recognised) form phases
(F2) and blocks (F2A) of their project; a phase includes its blocks' cards. A complete PHASE also gets a
decision card "Beslissing: fase X naar productie?" (valid question format, ⭐ advice) so it appears in #vragen;
production only after the owner's click. ONE message "🏁 [Project] Fase/blok <code> compleet op staging" goes to #meldingen
(ping) only when the phase has a closing card with "eindcontrole" in its title, every non-archived card with
the code is done, and every merged card is announced (see complete_phases). Phases already complete on the
first run are recorded silently (state staging-phases.json).
Staging URL/branch per project: flow config projecten[].staging_url/staging_branch when set, otherwise the
"- Staging: … (branch `x`) … URL https://…" line of the project file. Texts: flow config teksten.staging.

The first run (no state file) posts ONE summary of every merged card not yet announced, then
announces per card. State in ~/.hermes/state/. ``--dry-run`` prints and records nothing.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discord_post as dp  # noqa: E402
import flow_config as fc  # noqa: E402
from team_projects import HOME, PROJECTS_DIR, active_projects, boards, kanban, project_of  # noqa: E402

STATE = HOME / "state" / "staging-announced.json"
PHASES = HOME / "state" / "staging-phases.json"
# Card codes (TEAM.md): F<fase>[<blok>]-<nr>, e.g. F2A-03 (card in block F2A of phase F2), F2-37 (card directly
# in phase F2). Older codes are still recognised: "2A-0" → F2A, "2-37" → F2, "F1-07" → F1, "F3-00" → F3.
PHASE_RE = re.compile(r"^\s*F?(\d{1,2})([A-Z]?)-\w")
HERMES = fc.hermes_bin()


def card_code(title):
    """``(phase, block_or_None)`` from a card title, e.g. ("F2", "F2A"), or None."""
    m = PHASE_RE.match(title or "")
    if not m:
        return None
    phase = f"F{int(m.group(1))}"
    return phase, (phase + m.group(2)) if m.group(2) else None
STAGING_LINE = re.compile(r"^\s*-\s*Staging:.*?\(branch `([^`]+)`\).*?URL\s+(https?://\S+)", re.M)


def staging_of(slug, project):
    """``(repo, branch, url)`` from the flow config (when it names a staging URL) or the project file, or None."""
    cfg = fc.projects().get(slug) or {}
    if fc.is_set(cfg.get("staging_url")):
        repo = Path(project["paths"][0]) if project["paths"] else cfg.get("repo")
        return (Path(repo), cfg.get("staging_branch") or "staging", str(cfg["staging_url"]).rstrip("/")) if repo else None
    f = PROJECTS_DIR / f"{slug}.md"
    m = STAGING_LINE.search(f.read_text(encoding="utf-8", errors="replace")) if f.exists() else None
    if not m or not project["paths"]:
        return None
    return Path(project["paths"][0]), m.group(1), m.group(2).rstrip(".,)")


def on_remote_staging(repo, branch, commit):
    return subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", commit,
                           f"refs/remotes/origin/{branch}"], capture_output=True, timeout=30).returncode == 0


def merged_cards():
    projects = active_projects()
    found = []
    for board, db in boards():
        conn = kanban(db)
        for t in conn.execute("SELECT id, title, project_id, workspace_path, completed_at FROM tasks "
                              "WHERE status = 'done' ORDER BY completed_at"):
            slug = project_of(t, projects)
            staging = slug and staging_of(slug, projects[slug])
            if not staging:
                continue
            run = conn.execute("SELECT metadata FROM task_runs WHERE task_id = ? AND outcome = 'completed' "
                               "ORDER BY id DESC LIMIT 1", (t["id"],)).fetchone()
            try:
                commit = (json.loads(run["metadata"]) or {}).get("merged_commit") if run and run["metadata"] else None
            except (ValueError, TypeError):
                commit = None
            if not commit or not re.fullmatch(r"[0-9a-f]{7,40}", str(commit)):
                continue
            repo, branch, url = staging
            if not on_remote_staging(repo, branch, commit):
                continue  # not pushed/fetched yet: retry on a later run
            found.append({"key": f"{board}:{t['id']}", "id": t["id"], "title": t["title"],
                          "url": url, "commit": str(commit)[:7], "project": projects[slug]["name"]})
    return found


def line(card):
    return fc.text("staging.regel", project=card["project"], id=card["id"], titel=card["title"], url=card["url"])


def merged_commit(conn, task_id):
    run = conn.execute("SELECT metadata FROM task_runs WHERE task_id = ? AND outcome = 'completed' "
                       "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    try:
        return (json.loads(run["metadata"]) or {}).get("merged_commit") if run and run["metadata"] else None
    except (ValueError, TypeError):
        return None


def complete_phases(announced):
    """``{"<board>:<slug>:<code>": ("<Project>", "<eindcontrole-kaart>")}`` for phases/blocks that are really done.

    The board only knows the cards that exist, not the work still to come, so a phase or block counts as
    complete only when (1) it has its own closing card with "eindcontrole" in the title (a block's in that
    block, a phase's directly in the phase, e.g. "F2-99 Eindcontrole fase 2"), (2) every non-archived card of it
    is done (a phase includes the cards of its blocks), and (3) every merged card is already announced on
    staging. A lone card ("2-37 …") or a spec card ("F3-00 …") is never a complete phase (false "Fase 2/F3
    compleet" on 30-09)."""
    projects = active_projects()
    groups = {}
    for board, db in boards():
        conn = kanban(db)
        for t in conn.execute("SELECT id, title, status, project_id, workspace_path FROM tasks "
                              "WHERE status != 'archived'"):
            slug = project_of(t, projects)
            code = slug and card_code(t["title"])
            if not code:
                continue
            phase, block = code
            merged = merged_commit(conn, t["id"])
            ok = t["status"] == "done" and (not merged or f"{board}:{t['id']}" in announced)
            eind = "eindcontrole" in (t["title"] or "").lower()
            for grp in [g for g in (phase, block) if g]:
                g = groups.setdefault(f"{board}:{slug}:{grp}", {"name": projects[slug]["name"], "ok": True, "eind": None,
                                                                 "slug": slug, "board": board})
                g["ok"] = g["ok"] and ok
                if eind and (block or phase) == grp:  # the closing card belongs to its own level only
                    g["eind"] = t["id"]
    return {k: (v["name"], v["eind"]) for k, v in groups.items() if v["ok"] and v["eind"]}


def is_phase(key):
    return re.fullmatch(r"F\d+", key.rsplit(":", 1)[1]) is not None


def production_question(key, name, eind):
    """A complete PHASE (not a block) gets a decision card "Fase X naar productie?" in #vragen (once).
    Production still happens only after the owner's click; the manager then merges exactly that commit."""
    board, slug, code = key.split(":")
    project = active_projects().get(slug)
    staging = project and staging_of(slug, project)
    commit, url, branch = "?", "?", "staging"
    if staging:
        repo, branch, url = staging
        r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short=12", f"refs/remotes/origin/{branch}"],
                           capture_output=True, text=True, timeout=30)
        commit = r.stdout.strip() or "?"
    reason = fc.text("productievraag", prefix=fc.question_prefix(), code=code, project=name, eind=eind, url=url,
                     branch=branch, commit=commit)
    env = {"HOME": str(Path.home()), "PATH": f"{Path.home()}/.local/bin:/usr/bin:/bin"}
    if os.environ.get("HERMES_HOME"):
        env["HERMES_HOME"] = os.environ["HERMES_HOME"]  # a test with a fake home never touches the live board
    cli = [HERMES, "kanban", "--board", board]
    pid = next(iter(project["ids"])) if project and project["ids"] else None
    out = subprocess.run(cli + ["create", f"Beslissing: fase {code} naar productie?", "--body",
                                f"Automatisch na eindcontrole {eind} (staging-announce.py).", "--workspace", "scratch",
                                "--created-by", "workflow", "--idempotency-key", f"productie-{key}-{eind}", "--json"]
                         + (["--project", pid] if pid else []), capture_output=True, text=True, timeout=120, env=env)
    card = json.loads(out.stdout)["id"]
    subprocess.run(cli + ["block", "--kind", "needs_input", card, reason], capture_output=True, timeout=120, env=env)
    return card


def summary(cards):
    urls = sorted({c["url"] for c in cards})
    rows = [f"- {c['id']} {c['title']} (commit {c['commit']})" for c in cards]
    return "\n".join([fc.text("staging.samenvatting", aantal=len(cards), urls=", ".join(urls)), *rows])


def send(text):
    return dp.send(dp.channels()["staging"], text, silent=True)


def announce_phases(seen, dry_run=False):
    done = complete_phases(seen)
    if not PHASES.exists():
        if not dry_run:
            PHASES.write_text(json.dumps(sorted(done)))  # baseline: no messages for old phases
        return
    told = set(json.loads(PHASES.read_text() or "[]"))
    for key in sorted(set(done) - told):
        name, eind = done[key]
        level = "Fase" if is_phase(key) else "Blok"
        text = fc.text("staging.compleet", project=name, niveau=level, code=key.rsplit(":", 1)[1], eind=eind)
        if dry_run:
            print(text + (" + productievraag in #vragen" if is_phase(key) else ""))
            continue
        if not dp.event_seen(f"fase:{key}:{eind}"):
            msg = dp.send(dp.channels()["meldingen"], text, ping=True)
            dp.event_mark(f"fase:{key}:{eind}", channel=msg["channel_id"], message=msg["id"])
        if is_phase(key) and not dp.event_seen(f"productie:{key}:{eind}"):
            card = production_question(key, name, eind)
            dp.event_mark(f"productie:{key}:{eind}", card=card)
        told.add(key)
        PHASES.write_text(json.dumps(sorted(told)))


def main():
    _lock = dp.single_instance("staging-announce")  # noqa: F841 (held until exit)
    cards = merged_cards()
    first = not STATE.exists()
    seen = set() if first else set(json.loads(STATE.read_text() or "[]"))
    new = [c for c in cards if c["key"] not in seen]
    if "--dry-run" in sys.argv:
        print(summary(new) if first and new else "\n".join(line(c) for c in new))
        announce_phases(seen | {c["key"] for c in new}, dry_run=True)
        return
    if first and new:
        send(summary(new)[:1990])
    else:
        for card in new:
            if not dp.event_seen(f"{card['id']}:staging"):  # shared register: never twice, whoever asks
                msg = send(line(card))
                dp.event_mark(f"{card['id']}:staging", channel=msg["channel_id"], message=msg["id"], card=card["id"])
            seen.add(card["key"])  # recorded per message, so a failed send is retried
            STATE.write_text(json.dumps(sorted(seen)))
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(sorted(seen | {c["key"] for c in new})))
    announce_phases(seen | {c["key"] for c in new})


if __name__ == "__main__":
    main()
