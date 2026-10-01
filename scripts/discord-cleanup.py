#!/usr/bin/env python3
"""Discord cleanup check (cron, --no-agent, every 10 min). Keeps the channels in step with the board:

1. #vragen: every post (open or archived) whose card is done or archived gets the tag "verwerkt" and is
   archived + locked, also when a later message reopened it. Posts of other cards are left alone.
2. Kanban notification subscriptions to Discord are removed: routine kanban messages ("👀 ready for review",
   "✔ done") stay out of Discord (kanban.auto_subscribe_on_create is off; this catches explicit ones).
   Failures reach #meldingen through kanban-stuck-check.py.
3. #meldingen: a stuck-card message whose situation is over is edited to "✅ opgelost (HH:MM) — …"
   (without ping); a "kaart mislukt" message once the card is done or archived.
Uses the event register ~/.hermes/state/discord-events.json. ``--dry-run`` prints and changes nothing.
"""
import importlib.util
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discord_post as dp  # noqa: E402
import flow_config as fc  # noqa: E402
from team_projects import HOME, boards, kanban  # noqa: E402

HERMES = fc.hermes_bin()
CARD_RE = re.compile(r"\bt_[0-9a-f]{8}\b")


def card_status(task_id):
    for _board, db in boards() + [("default", HOME / "kanban.db")]:
        if Path(db).exists():
            row = kanban(db).execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row:
                return row["status"]
    return None


def forum_posts(forum):
    posts = {t["id"]: t for t in dp.api("GET", f"/guilds/{dp.channels()['guild']}/threads/active")["threads"]
             if t.get("parent_id") == forum}
    before = None
    while True:
        r = dp.api("GET", f"/channels/{forum}/threads/archived/public?limit=100" + (f"&before={before}" if before else ""))
        for t in r.get("threads", []):
            posts.setdefault(t["id"], t)
        if not r.get("has_more") or not r.get("threads"):
            return list(posts.values())
        before = r["threads"][-1]["thread_metadata"]["archive_timestamp"]


def close_done_posts(forum, dry):
    tags = {t["name"].lower(): t["id"] for t in dp.api("GET", f"/channels/{forum}")["available_tags"]}
    fixed = []
    for t in forum_posts(forum):
        m = CARD_RE.search(t["name"])
        if not m or card_status(m.group(0)) not in ("done", "archived"):
            continue
        md = t["thread_metadata"]
        if md.get("archived") and md.get("locked") and tags.get(fc.tag("verwerkt").lower()) in t.get("applied_tags", []):
            continue
        fixed.append(t["name"])
        if not dry:
            if md.get("archived"):
                dp.api("PATCH", f"/channels/{t['id']}", {"archived": False})
            dp.set_status(t["id"], forum, "verwerkt", close=True)
    return fixed


def drop_discord_subscriptions(dry):
    dropped = []
    for board, db in boards() + [("default", HOME / "kanban.db")]:
        if not Path(db).exists():
            continue
        for s in kanban(db).execute("SELECT task_id, chat_id, thread_id FROM kanban_notify_subs WHERE platform = 'discord'"):
            dropped.append(f"{s['task_id']}→{s['thread_id'] or s['chat_id']}")
            if not dry:
                cmd = [HERMES, "kanban", "--board", board, "notify-unsubscribe", s["task_id"], "--platform", "discord",
                       "--chat-id", s["chat_id"]] + (["--thread-id", s["thread_id"]] if s["thread_id"] else [])
                subprocess.run(cmd, capture_output=True, timeout=120,
                               env={"HOME": str(Path.home()), "PATH": f"{Path.home()}/.local/bin:/usr/bin:/bin"})
    return dropped


def stuck_module():
    spec = importlib.util.spec_from_file_location("stuck", Path(__file__).with_name("kanban-stuck-check.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def guard_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("board_guard", Path(__file__).with_name("board-guard.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def resolve_messages(dry):
    now = time.time()
    current = {c["episode"] for c in stuck_module().stuck_cards(now) if not c.get("quiet")}
    guard_now = {f"guard:{a['id']}:{a['kind']}" for a in guard_module().anomalies(now)}
    grouped = {}
    for k, v in dp.events_matching("").items():
        if v.get("grouped"):
            grouped.setdefault(v["grouped"], []).append(k.split(":", 1)[0])
    resolved = []
    for key, info in dp.events_matching("").items():
        if not info.get("message") or info.get("resolved") or not info.get("text"):
            continue
        if ":vastgelopen:" in key:
            over = info.get("episode") not in current
        elif ":mislukt:" in key:
            over = card_status(info.get("card", "")) in ("done", "archived")
        elif ":workflow-open" in key or ":ongepost:" in key:
            over = True  # retired message types (workflow items left the board; unposted questions: board-guard)
        elif key.startswith("guard:"):
            over = key not in guard_now  # the manager fixed it (audit S4)
        elif key.startswith("bouwer-onbeschikbaar:"):
            over = all(card_status(c) != "blocked" for c in grouped.get(key, [])) and \
                now - float(info.get("at", now)) > 3600
        else:
            continue
        if not over:
            continue
        resolved.append(key)
        if not dry:
            text = re.sub(r"^<@\d+>\s*", "", info["text"])
            stamp = datetime.now(fc.tz()).strftime("%H:%M")
            try:
                dp.edit(info["channel"], info["message"], fc.text("opgelost", tijd=stamp, tekst=text[:1800]))
            except RuntimeError:
                pass  # message already gone
            dp.event_mark(key, resolved=time.time())
    return resolved


def main():
    _lock = dp.single_instance("discord-cleanup")  # noqa: F841 (held until exit)
    dry = "--dry-run" in sys.argv
    if "vragen" not in dp.channels():
        print("geen #vragen-kanaal ingesteld (draai eerst discord_setup.py); niets te controleren")
        return
    forum = dp.channels()["vragen"]
    report = {"posts_gesloten": close_done_posts(forum, dry), "abonnementen_afgemeld": drop_discord_subscriptions(dry),
              "meldingen_opgelost": resolve_messages(dry)}
    if dry or any(report.values()):
        print(report)


if __name__ == "__main__":
    main()
