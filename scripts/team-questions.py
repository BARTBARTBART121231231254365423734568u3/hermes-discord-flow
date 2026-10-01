#!/usr/bin/env python3
"""Owner questions to Discord (cron, --no-agent, every 2 min).

Every card of an active or paused project (STATUS ACTIEF or GEPAUZEERD, flow config vragen.statussen; paused = intake
questions) that is blocked with needs_input and a valid reason starting with the question prefix (flow config
vragen.prefix, e.g. "Vraag voor de eigenaar:") gets its own post in the #vragen forum, with a ping:
  title "[Project] <id> <short title>", tags <project> + open.
Format (TEAM.md, "Vragen aan de eigenaar"):
  Wat speelt er: … / Vraag: … /
  Optie N[ ⭐ Aanbevolen]: <titel> with Inhoud / Voordeel / Nadeel / Daarna on the lines below /
  Waarom deze aanbeveling: 2–3 redenen … Kies liever optie X als … / Zonder antwoord: … / Details: …
Exactly one option carries ⭐ Aanbevolen; its button comes first and has ⭐ in the label (custom_id
"tq:<task>:<episode>:<n>"; the gateway's fork handler accepts only the owner's click). A recommendation is
advice only: nothing happens until the owner clicks. Details go in a second message.

Validation: a question without ≥2 complete options, without exactly one recommendation or without the
"Waarom deze aanbeveling" block (incl. when another option is better) is NOT posted; it goes back to
the sender: a comment on the card lists what is missing, and the card is unblocked to its assignee
(a card without assignee goes to the manager profile "default"). A needs_input reason without the
prefix is treated the same way. ``--check`` validates a reason from stdin (exit 1 + what is missing).

One post per card: a new question on the same card (new block event) is posted as a new message in the
existing post, which is reopened and tagged open again. Every question has a button "✏️ Anders…" that opens
a text field (fork handler). Event keys "<kaart>:vraag:<blokkade-event>" in ~/.hermes/state/discord-events.json
make sure a question is never posted twice. A reason starting with "Wacht op" is a wait status written by
the manager after an answer (not a question) and is skipped; a new real question on a card that waits on a
workflow run (flow config paden.workflow_waits) is posted normally.

Status per post, checked every run: open → beantwoord (the owner clicked a button, or the card is no longer
blocked on that question; typed text alone does not count, it may be a counter-question) → verwerkt, archived and locked once the card
is done or archived (discord-cleanup.py re-checks every 10 minutes). State: questions-posts.json (per card).

``--dry-run`` prints what would be posted or returned and records nothing.
``--advies <kaart> <n> "<waarom>"``: the team's advice changed → old buttons off, new message with ⭐ on option n.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discord_post as dp  # noqa: E402
import flow_config as fc  # noqa: E402
from team_projects import HOME, active_projects, boards, kanban, project_of  # noqa: E402

PREFIX = fc.question_prefix().lower()
STATE = HOME / "state" / "questions-sent.json"
POSTS = HOME / "state" / "questions-posts.json"
HERMES = fc.hermes_bin()
T = fc.get("teksten.vraag")
MAX_CONTENT = 1900
TOP = {"wat speelt er": "wat", "vraag": "vraag", "waarom deze aanbeveling": "waarom",
       "zonder antwoord": "zonder", "details": "details"}
SUB = {"inhoud": "inhoud", "voordeel": "voordeel", "nadeel": "nadeel", "daarna": "daarna"}
OPTION_HEAD = re.compile(r"^\s*optie\s+(\d{1,2})\s*(⭐\s*aanbevolen)?\s*:\s*(.*)$", re.I)
FIELD_LINE = re.compile(r"^\s*[-*]?\s*([a-z ]+?)\s*:\s*(.*)$", re.I)


def open_questions():
    # Paused projects too: their intake and "Beslissing: …" questions must reach the owner before they become ACTIEF.
    projects = active_projects(tuple(fc.get("vragen.statussen")))
    found = []
    for board, db in boards():
        conn = kanban(db)
        for t in conn.execute("SELECT id, title, assignee, project_id, workspace_path FROM tasks "
                              "WHERE status = 'blocked' AND block_kind = 'needs_input'"):
            slug = project_of(t, projects)
            if not slug:
                continue
            row = conn.execute("SELECT id, payload FROM task_events WHERE task_id = ? AND kind = 'blocked' "
                               "ORDER BY id DESC LIMIT 1", (t["id"],)).fetchone()
            try:
                reason = (json.loads(row["payload"]) or {}).get("reason", "") if row else ""
            except (ValueError, TypeError):
                reason = ""
            asked = reason.strip().lower().startswith(PREFIX)
            found.append({"id": t["id"], "title": t["title"], "project": slug, "assignee": t["assignee"],
                          "project_name": projects[slug]["name"], "board": board, "asked": asked,
                          "question": reason.strip()[len(PREFIX):].strip() if asked else reason.strip(),
                          "event": row["id"] if row else 0,
                          "episode": f"{board}:{t['id']}:{row['id'] if row else 0}"})
    return found


def parse_question(text):
    """``{"wat", "vraag", "opties": [{"n", "titel", "aanbevolen", "inhoud", "voordeel", "nadeel", "daarna"}],
    "waarom", "zonder", "details"}`` from the fielded format; unknown lines continue the previous field."""
    out, options, current, field = {}, [], None, None
    for line in text.splitlines():
        head = OPTION_HEAD.match(line)
        if head:
            current = {"n": int(head.group(1)), "titel": head.group(3).strip(), "aanbevolen": bool(head.group(2))}
            options.append(current)
            field = ("opt", "titel")
            continue
        m = FIELD_LINE.match(line)
        key = m.group(1).strip().lower() if m else None
        if current is not None and key in SUB:
            current[SUB[key]] = m.group(2).strip()
            field = ("opt", SUB[key])
        elif key in TOP:
            out[TOP[key]] = m.group(2).strip()
            field, current = ("top", TOP[key]), (None if TOP[key] != "details" else None)
        elif field and line.strip():
            kind, name = field
            target = current if kind == "opt" and current is not None else out
            target[name] = (target.get(name, "") + "\n" + line.strip()).strip()
    out["opties"] = options
    return out


def validate(q):
    """List of what is missing (empty = the question may be posted)."""
    missing = []
    f = parse_question(q)
    if not f.get("vraag"):
        missing.append("de regel 'Vraag:'")
    opts = f["opties"]
    if len(opts) < 2:
        missing.append("minimaal twee opties ('Optie 1: …', 'Optie 2: …')")
    for o in opts:
        gaps = [k for k in ("inhoud", "voordeel", "nadeel", "daarna") if not o.get(k)]
        if not o["titel"]:
            gaps.insert(0, "titel")
        if gaps:
            missing.append(f"optie {o['n']} mist: {', '.join(gaps)}")
    stars = [o for o in opts if o["aanbevolen"]]
    if len(stars) != 1:
        missing.append(f"precies één optie met '⭐ Aanbevolen' (nu {len(stars)})")
    why = f.get("waarom", "")
    if not why:
        missing.append("het blok 'Waarom deze aanbeveling:' (2–3 redenen)")
    elif "kies liever" not in why.lower():
        missing.append("in 'Waarom deze aanbeveling' wanneer je beter een andere optie kiest ('Kies liever optie X als …')")
    if not f.get("zonder"):
        missing.append("de regel 'Zonder antwoord:'")
    paths = sorted(set(re.findall(r"(?:~/|/home/|/tmp/|/var/|/etc/|/root/)[^\s,;)'\"]*", q)))
    if paths:
        missing.append("geen serverpaden (" + ", ".join(paths[:3]) + f"): {fc.owner_name()} kan die niet openen; zet de kern in "
                       "de vraag zelf of link naar het bestand op GitHub")
    return missing


def title(q):
    short = re.sub(r"\s+", " ", q["title"])
    return f"[{q['project_name']}] {q['id']} {short}"[:100]


def ordered_options(f):
    return sorted(f["opties"], key=lambda o: (not o["aanbevolen"], o["n"]))


def layout(q):
    """``(content, details, components)`` for a valid question: recommended option first."""
    f = parse_question(q["question"])
    opts = ordered_options(f)

    def block(o, full):
        star = "⭐ " if o["aanbevolen"] else ""
        head = f"**{star}{o['n']}. {o['titel']}**" + (" _(aanbevolen)_" if o["aanbevolen"] else "")
        lines = [head]
        if full:
            lines.append(f"  {o['inhoud']}")
        lines.append(f"  ✅ {o['voordeel']}  ·  ⚠️ {o['nadeel']}")
        lines.append(f"  ➡️ {o['daarna']}")
        return "\n".join(lines)

    def compose(full):
        lines = [f"📌 {f['wat']}"] if f.get("wat") else []
        lines.append(f"❓ **{f['vraag']}**")
        lines += [block(o, full) for o in opts]
        lines.append(f"{T['waarom_kop']} {f['waarom']}")
        lines.append(f"⏸️ {f['zonder']}")
        lines.append(T["voetregel"])
        return "\n".join(lines)

    content, details = compose(True), f.get("details", "")
    if len(content) > MAX_CONTENT:  # phone first: short version above, full options in the details message
        content = compose(False)
        extra = "\n".join(f"**{o['n']}. {o['titel']}**: {o['inhoud']}" for o in opts)
        details = f"{extra}\n\n{details}".strip()
    rows, row = [], []
    for o in opts[:25]:
        label = f"{'⭐ ' if o['aanbevolen'] else ''}{o['n']} · {o['titel']}"[:80]
        row.append({"type": 2, "style": 3 if o["aanbevolen"] else 2, "label": label,
                    "custom_id": f"tq:{q['id']}:{q['event']}:{o['n']}"[:100]})
        if len(row) == 5:
            rows.append({"type": 1, "components": row})
            row = []
    other = {"type": 2, "style": 2, "label": T["knop_anders"][:80], "custom_id": f"tq:{q['id']}:{q['event']}:x"[:100]}
    if len(row) < 5:
        row.append(other)
    else:
        rows.append({"type": 1, "components": row})
        row = [other]
    rows.append({"type": 1, "components": row})
    return content[:2000 - 30], details, rows


def return_to_sender(q, missing):
    """Not posted: comment on the card with what is missing, then send the card back to its sender."""
    import subprocess
    text = (f"{fc.question_prefix().rstrip(':')} NIET gepost: het format is niet compleet (TEAM.md, 'Vragen aan {fc.owner_name()}'). "
            "Vul aan en blokkeer de kaart opnieuw. Ontbreekt: " + "; ".join(missing) + ".")
    cli = [HERMES, "kanban", "--board", q["board"]]
    subprocess.run(cli + ["comment", "--author", "workflow", q["id"], text], capture_output=True, timeout=120)
    if not q.get("assignee"):
        subprocess.run(cli + ["assign", q["id"], "default"], capture_output=True, timeout=120)
    subprocess.run(cli + ["unblock", "--reason", "vraag onvolledig, terug naar afzender", q["id"]],
                   capture_output=True, timeout=120)


def post(q, forum):
    """``(thread_id, question_message_id)``: a new post, or a new message in the card's existing post."""
    content, details, components = layout(q)
    existing = (dp.event_get(f"{q['id']}:post") or {}).get("thread")
    if existing:
        try:
            retire_previous_questions(q["id"], existing)
            dp.reopen(existing, forum)
            msg = dp.send(existing, content, ping=True, components=components)
            if details:
                dp.send(existing, f"{T['details_kop']}\n{details}", silent=True)
            return existing, msg["id"]
        except RuntimeError:
            pass  # post was deleted: start a new one
    tags = dp.ensure_tags(forum, [q["project_name"], *dp.STATUS_TAGS])
    thread = dp.forum_post(forum, title(q), content, [tags[q["project_name"].lower()[:20]], tags[fc.tag("open").lower()]],
                           ping=True, components=components)
    dp.event_mark(f"{q['id']}:post", thread=thread["id"])
    if details:
        dp.send(thread["id"], f"{T['details_kop']}\n{details}", silent=True)
    return thread["id"], thread["id"]  # the starter message has the thread's id


def card_status(board, task_id):
    for b, db in boards():
        if b == board:
            row = kanban(db).execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return row["status"] if row else "archived"
    return None


def reconcile(posts, still_open, forum, owner):
    """Advance tags of the card posts; returns the posts still to follow."""
    keep = {}
    for card, p in posts.items():
        try:
            p = reconcile_one(p, still_open, forum, owner)
        except RuntimeError as e:  # one bad post must not stop the others; retried next run
            if "10003" in str(e):  # post no longer exists (deleted): stop following it
                p = None
            else:
                print(f"post {p.get('thread')}: {e}", file=sys.stderr)
        if p is not None:
            keep[card] = p
    return keep


def reconcile_one(p, still_open, forum, owner):
    """The post's state after this run, or None once it is verwerkt."""
    status = card_status(p["board"], p["task"])
    if status in ("done", "archived"):
        remove_buttons(p, owner)
        dp.set_status(p["thread"], forum, "verwerkt", close=True)
        return None
    if p["status"] == "open":
        # Typed text alone is NOT an answer (owner decision 30-09): it can be a counter-question. The buttons stay until
        # the card leaves this question (unblocked, "Wacht op …", completed) or a button choice set "beantwoord".
        answered = p["episode"] not in still_open
        if not answered:
            thread = dp.api("GET", f"/channels/{p['thread']}")
            tag_names = {t["id"]: t["name"].lower() for t in dp.api("GET", f"/channels/{forum}")["available_tags"]}
            answered = fc.tag("beantwoord").lower() in {tag_names.get(t) for t in thread.get("applied_tags", [])}
        if answered:
            dp.set_status(p["thread"], forum, "beantwoord")
            remove_buttons(p, owner)
            p = {**p, "status": "beantwoord"}
    return p


def retire_previous_questions(card, thread):
    """A new question on the same card replaces the older ones: their buttons go off at once."""
    suffix = "\n\n" + T["vervangen"]
    for key, info in dp.events_matching(f"{card}:vraag:").items():
        if info.get("thread") != thread or not info.get("message"):
            continue
        try:
            msg = dp.api("GET", f"/channels/{thread}/messages/{info['message']}")
        except RuntimeError:
            continue
        if msg.get("components"):
            dp.api("PATCH", f"/channels/{thread}/messages/{info['message']}",
                   {"content": (msg.get("content") or "")[: 1900 - len(suffix)] + suffix, "components": [],
                    "allowed_mentions": {"parse": []}})


def remove_buttons(p, owner=""):
    """Answered another way (typed in the post, desktop/CLI, or the card is done): the question's buttons go
    off so a late click can never add a second answer (the fork handler also refuses it)."""
    try:
        msg = dp.api("GET", f"/channels/{p['thread']}/messages/{p['message']}")
    except RuntimeError:
        return
    if msg.get("components"):
        typed = any(m.get("author", {}).get("id") == owner and int(m["id"]) > int(p["message"])
                    for m in dp.thread_messages(p["thread"])) if owner else False
        suffix = "\n\n" + (T["beantwoord_in_post"] if typed else T["beantwoord_buiten"])
        dp.api("PATCH", f"/channels/{p['thread']}/messages/{p['message']}",
               {"content": (msg.get("content") or "")[: 1900 - len(suffix)] + suffix, "components": [],
                "allowed_mentions": {"parse": []}})


def waiting_cards():
    try:
        return set(json.loads(fc.path("workflow_waits").read_text()))
    except (OSError, ValueError):
        return set()


def is_wait_status(q):
    """A "Wacht op …" reason is a wait state, not a question. A card that waits on a workflow run
    (workflow-waits.json) can still get a NEW real question: that one is posted like any other."""
    return q["question"].strip().lower().startswith("wacht op")


def change_advice(card, choice, why):
    """The team's advice changed (e.g. after a follow-up question): old buttons off, a new message with the ⭐
    on option <choice>, new buttons and the new "Waarom". Nothing is decided; the owner still chooses."""
    import subprocess
    if "kies liever" not in why.lower():
        sys.exit("ONVOLLEDIG: 'waarom' moet ook zeggen wanneer een andere optie beter is ('Kies liever optie X als …')")
    asked = sorted(((int(k.rsplit(":", 1)[1]), v) for k, v in dp.events_matching(f"{card}:vraag:").items()
                    if v.get("message") and v.get("thread")), key=lambda kv: kv[0])
    if not asked:
        sys.exit(f"geen geposte vraag gevonden voor {card}")
    event, info = asked[-1]
    q = next((x for x in open_questions() if x["id"] == card and x["event"] == event), None)
    if not q:
        sys.exit(f"de vraag van {card} staat niet meer open (beantwoord of kaart niet meer geblokkeerd)")
    f = parse_question(q["question"])
    if choice not in {o["n"] for o in f["opties"]}:
        sys.exit(f"optie {choice} bestaat niet")
    lines = []
    for line in q["question"].splitlines():
        head = OPTION_HEAD.match(line)
        if head:
            n, title_ = int(head.group(1)), head.group(3).strip()
            line = f"Optie {n}{' ⭐ Aanbevolen' if n == choice else ''}: {title_}"
        elif re.match(r"^\s*waarom deze aanbeveling\s*:", line, re.I):
            line = f"Waarom deze aanbeveling: {why}"
        lines.append(line)
    q = {**q, "question": "\n".join(lines)}
    content, _details, components = layout(q)
    forum, thread, old = dp.channels()["vragen"], info["thread"], info["message"]
    try:
        old_msg = dp.api("GET", f"/channels/{thread}/messages/{old}")
        dp.api("PATCH", f"/channels/{thread}/messages/{old}",
               {"content": (old_msg["content"][:1850] + "\n\n" + T["advies_gewijzigd"]),
                "components": [], "allowed_mentions": {"parse": []}})
    except RuntimeError:
        pass  # old message gone: just post the new advice
    dp.reopen(thread, forum)
    msg = dp.send(thread, T["nieuw_advies"] + "\n" + content, ping=True, components=components)
    dp.event_mark(f"{card}:vraag:{event}", thread=thread, message=msg["id"])
    dp.event_mark(f"{card}:advies:{event}:{choice}:{msg['id']}", thread=thread, message=msg["id"])
    posts = json.loads(POSTS.read_text() or "{}") if POSTS.exists() else {}
    for k, v in posts.items():
        if v.get("task") == card:
            posts[k] = {**v, "message": msg["id"], "status": "open"}
    POSTS.write_text(json.dumps(posts))
    subprocess.run([HERMES, "kanban", "--board", q["board"], "comment", "--author", "manager", card,
                    f"Advies gewijzigd naar optie {choice}: {why} (post bijgewerkt; oude knoppen uit)"],
                   capture_output=True, timeout=120)
    print(f"advies voor {card} gewijzigd naar optie {choice}; nieuwe knoppen in de post")


def problems_of(q):
    if not q["asked"]:
        return [f"de reden begint niet met '{fc.question_prefix()}' (een needs_input-blokkade is altijd een vraag in het format)"]
    return validate(q["question"])


def main():
    _lock = dp.single_instance("team-questions")  # noqa: F841 (held until exit)
    if "--advies" in sys.argv:
        i = sys.argv.index("--advies")
        if len(sys.argv) < i + 4:
            sys.exit('gebruik: team-questions.py --advies <kaart-id> <optienummer> "<waarom, incl. Kies liever optie X als …>"')
        change_advice(sys.argv[i + 1], int(sys.argv[i + 2]), sys.argv[i + 3])
        return
    if "--check" in sys.argv:
        text = sys.stdin.read().strip()
        text = text[len(PREFIX):].strip() if text.lower().startswith(PREFIX) else text
        missing = validate(text)
        print("OK: vraag mag gepost worden" if not missing else "ONVOLLEDIG:\n- " + "\n- ".join(missing))
        sys.exit(1 if missing else 0)
    questions = open_questions()
    if "--dry-run" in sys.argv:
        for q in questions:
            if is_wait_status(q):
                print(f"# {title(q)}\nOVERGESLAGEN: wachtstatus of wacht op een workflowrun", end="\n\n")
                continue
            missing = problems_of(q)
            if missing:
                print(f"# {title(q)}\nTERUG NAAR AFZENDER: {'; '.join(missing)}", end="\n\n")
                continue
            content, details, components = layout(q)
            print(f"# {title(q)}\n{content}\n[knoppen: {[c['label'] for r in components for c in r['components']]}]"
                  + (f"\n-- details --\n{details}" if details else ""), end="\n\n")
        return
    ch = dp.channels()
    forum, owner = ch["vragen"], ch.get("owner", "")
    posts = json.loads(POSTS.read_text() or "{}") if POSTS.exists() else {}
    posts = {k: v for k, v in posts.items() if "thread" in v and k.count(":") == 1}  # per card: "board:task"
    for old in json.loads(STATE.read_text() or "[]") if STATE.exists() else []:  # migrate old episode keys
        board, task, event = old.split(":")
        if not dp.event_seen(f"{task}:vraag:{event}"):
            dp.event_mark(f"{task}:vraag:{event}", migrated=True)
    open_eps = set()
    for q in questions:
        if is_wait_status(q):
            continue
        key = f"{q['id']}:vraag:{q['event']}"
        open_eps.add(key)
        if dp.event_seen(key):
            continue
        missing = problems_of(q)
        if missing:
            return_to_sender(q, missing)
            dp.event_mark(key, returned="; ".join(missing))
            continue
        thread, message = post(q, forum)
        dp.event_mark(key, thread=thread, message=message)
        posts[f"{q['board']}:{q['id']}"] = {"thread": thread, "message": message, "task": q["id"],
                                            "board": q["board"], "status": "open", "episode": key}
        POSTS.parent.mkdir(parents=True, exist_ok=True)
        POSTS.write_text(json.dumps(posts))
    posts = reconcile(posts, open_eps, forum, owner)
    POSTS.parent.mkdir(parents=True, exist_ok=True)
    POSTS.write_text(json.dumps(posts))


if __name__ == "__main__":
    main()
