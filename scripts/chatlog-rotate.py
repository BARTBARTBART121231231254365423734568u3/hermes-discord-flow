#!/usr/bin/env python3
"""chatlog-rotate.py [--dry-run]: nightly new #chatlog session with a short handoff (opruimronde ronde 3).

Hermes keeps the #chatlog conversation with the manager in one session for days; every call then sends
~100k tokens of old context (vs ~33k for a fresh session) and old statements linger. Run by the systemd
--user timer hermes-chatlog-rotate.timer (04:00, retried 05:00 and 06:00):
1. Finds the open manager session of the #chatlog channel (session_key agent:main:discord:group:<chatlog>:…).
2. Skips when it is younger than 20 hours, or active in the last 15 minutes (tried again at the next slot).
3. Writes the handoff (flow config paden.overdracht_chatlog; the previous one is kept as .vorige): the owner's messages of
   the last 24 hours (literal, trimmed), the manager's last answer, and a pointer to team-status.py for the
   live state. No model, so nothing is summarised away; decisions live on cards anyway (TEAM.md).
4. Ends the session in state.db (SessionDB.end_session, reason "nightly_rotate"). The gateway self-heals
   routing for an ended session, so the owner's next message starts a fresh session; the manager SOUL says to
   read the handoff first in a conversation without earlier messages.
Nothing is posted to Discord. Log: ~/.hermes/logs/chatlog-rotate.log.

``--titel`` (timer hermes-chatlog-titel, every 15 min): gives the open #chatlog session that started after
the last rotation the fixed title "#chatlog <dd-mm>" (start date, NL), via `hermes sessions rename`; a manual
title is never overwritten by Hermes' automatic titles. Sessions from before the first rotation are left alone.
"""
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import flow_config as fc  # noqa: E402

H = fc.HOME
NL = fc.tz()
OWNER = fc.owner_name()
HANDOFF = fc.path("overdracht_chatlog")
ROTATED = H / "state" / "chatlog-rotate.json"
LOG = H / "logs" / "chatlog-rotate.log"
MIN_AGE = 20 * 3600
QUIET = 15 * 60
MAX_MSG = 600
MAX_TOTAL = 12000


def log(line):
    if "--dry-run" in sys.argv:  # a dry run writes nothing, also no log line
        print(line)
        return
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(f"{datetime.now(NL).isoformat(timespec='seconds')} {line}\n")
    print(line)


def fmt(ts):
    return datetime.fromtimestamp(ts, NL).strftime("%d-%m %H:%M")


def set_title(dry):
    """Fixed title "#chatlog <dd-mm>" for the open #chatlog session started after the last rotation."""
    import subprocess
    try:
        rotated = float(json.loads(ROTATED.read_text())["at"])
    except (OSError, ValueError, KeyError):
        return 0  # no rotation yet: leave the old session's title alone
    chatlog = fc.channel_ids()["chatlog"]
    conn = sqlite3.connect(f"file:{H / 'state.db'}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    s = conn.execute("SELECT id, started_at, title FROM sessions WHERE ended_at IS NULL AND session_key LIKE ? "
                     "AND started_at > ? ORDER BY started_at DESC LIMIT 1",
                     (f"agent:main:discord:group:{chatlog}:%", rotated)).fetchone()
    if not s:
        return 0
    want = f"#chatlog {datetime.fromtimestamp(s['started_at'], NL):%d-%m}"
    if (s["title"] or "") == want or (s["title"] or "").startswith(want + " #"):
        return 0
    if dry:
        print(f"DRY-RUN: zou {s['id']} hernoemen naar '{want}' (nu: {s['title']!r})")
        return 0
    r = subprocess.run([fc.hermes_bin(), "sessions", "rename", s["id"], want],
                       capture_output=True, text=True, timeout=60)
    log(f"titel {s['id']} → '{want}' (exit {r.returncode})")
    return 0


def main():
    dry = "--dry-run" in sys.argv
    if "--titel" in sys.argv:
        return set_title(dry)
    now = time.time()
    chatlog = fc.channel_ids()["chatlog"]
    conn = sqlite3.connect(f"file:{H / 'state.db'}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    s = conn.execute("SELECT id, started_at, last_activity_at FROM sessions WHERE ended_at IS NULL "
                     "AND session_key LIKE ? ORDER BY started_at DESC LIMIT 1",
                     (f"agent:main:discord:group:{chatlog}:%",)).fetchone()
    if not s:
        log("geen open #chatlog-sessie; niets te doen")
        return 0
    age, idle = now - s["started_at"], now - (s["last_activity_at"] or s["started_at"])
    if age < MIN_AGE:
        log(f"{s['id']}: pas {age / 3600:.1f} uur oud; niet vernieuwd")
        return 0
    if idle < QUIET:
        log(f"{s['id']}: actief {idle / 60:.0f} min geleden; volgende poging later")
        return 0
    seen, users = set(), []
    for m in conn.execute("SELECT content, timestamp FROM messages WHERE session_id = ? AND role = 'user' "
                          "AND timestamp > ? ORDER BY timestamp, id", (s["id"], now - 24 * 3600)):
        text = (m["content"] or "").strip()
        key = (int(m["timestamp"]), text[:200])
        if not text or text.startswith("[CONTEXT COMPACTION") or key in seen:
            continue  # compaction re-inserts rows and adds summary rows: keep each real message once
        seen.add(key)
        users.append(m)
    last = conn.execute("SELECT content, timestamp FROM messages WHERE session_id = ? AND role = 'assistant' "
                        "AND content IS NOT NULL AND content != '' ORDER BY id DESC LIMIT 1", (s["id"],)).fetchone()
    lines = [f"# Overdracht #chatlog ({fmt(now)})", "",
             f"Het vorige gesprek ({s['id']}, gestart {fmt(s['started_at'])}) is 's nachts afgesloten om het "
             f"klein te houden. Dit is wat {OWNER} de laatste 24 uur schreef, en jouw laatste antwoord. De stand van "
             "het werk haal je live uit `python3 ~/.hermes/scripts/team-status.py`, niet uit deze tekst.", "",
             f"## Berichten van {OWNER} (laatste 24 uur)"]
    for m in users[-25:]:
        text = " ".join((m["content"] or "").split())
        lines.append(f"- {fmt(m['timestamp'])}: {text[:MAX_MSG]}{'…' if len(text) > MAX_MSG else ''}")
    if not users:
        lines.append("- (geen)")
    if last:
        text = (last["content"] or "").strip()
        lines += ["", f"## Jouw laatste antwoord ({fmt(last['timestamp'])})", text[:3000] + ("…" if len(text) > 3000 else "")]
    body = "\n".join(lines)[:MAX_TOTAL] + "\n"
    if dry:
        print(body)
        log(f"DRY-RUN: zou {s['id']} afsluiten ({age / 3600:.0f} uur oud, {len(users)} berichten van {OWNER})")
        return 0
    if HANDOFF.exists():
        HANDOFF.replace(HANDOFF.with_suffix(".md.vorige"))
    HANDOFF.write_text(body)
    sys.path.insert(0, str(H / "hermes-agent"))
    from hermes_state import SessionDB
    SessionDB(db_path=H / "state.db").end_session(s["id"], "nightly_rotate")
    ROTATED.write_text(json.dumps({"at": now, "ended": s["id"]}))
    log(f"{s['id']} afgesloten ({age / 3600:.0f} uur oud); overdracht {len(body)} tekens")
    return 0


if __name__ == "__main__":
    sys.exit(main())
