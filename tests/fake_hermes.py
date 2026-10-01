#!/usr/bin/env python3
"""A stand-in for the ``hermes`` command in the clean-install test.

It logs every call to $FAKE_HERMES_DIR/fake-hermes-calls.jsonl and implements just enough to let the flow scripts
work end to end on the scratch board (tests/kanban_fixture.py):
  hermes kanban --board B create|block|unblock|comment|assign|edit|notify-unsubscribe …
  hermes cron list | cron create --name … --script … [--no-agent] --deliver … <schedule> [prompt]
  hermes chat -Q -q <prompt>          (the manager being woken: only logged)
  hermes sessions rename <id> <title> (only logged)
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kanban_fixture as kf  # noqa: E402

HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
OWN = Path(os.environ.get("FAKE_HERMES_DIR") or HOME)  # where this fake keeps its call log and cron jobs
VALUED = {"--board", "--author", "--reason", "--kind", "--body", "--workspace", "--created-by", "--idempotency-key",
          "--project", "--priority", "--platform", "--chat-id", "--thread-id", "--name", "--script", "--deliver", "-q",
          "--parent", "--assignee"}


def parse(args):
    opts, pos, i = {}, [], 0
    while i < len(args):
        a = args[i]
        if a in VALUED:
            opts[a] = args[i + 1]
            i += 2
        elif a.startswith("-"):
            opts[a] = True
            i += 1
        else:
            pos.append(a)
            i += 1
    return opts, pos


def cron(args):
    f = OWN / "fake-cron.json"
    jobs = json.loads(f.read_text()) if f.exists() else []
    if args[:1] == ["list"]:
        for j in jobs:
            print(f"  {j['id']} [active]\n    Name:      {j['name']}\n    Schedule:  {j['schedule']}\n"
                  f"    Deliver:   {j['deliver']}\n    Script:    {j['script']}\n")
        return 0
    if args[:1] == ["create"]:
        opts, pos = parse(args[1:])
        job = {"id": f"job{len(jobs) + 1:03d}", "name": opts.get("--name"), "script": opts.get("--script"),
               "deliver": opts.get("--deliver"), "no_agent": bool(opts.get("--no-agent")), "schedule": pos[0],
               "prompt": pos[1] if len(pos) > 1 else None}
        jobs.append(job)
        f.write_text(json.dumps(jobs, ensure_ascii=False, indent=1))
        print(f"Created job {job['id']}")
        return 0
    return 2


def kanban(args):
    opts, pos = parse(args)
    db = kf.board_db(HOME, opts.get("--board", "team"))
    cmd, rest = pos[0], pos[1:]
    if cmd == "create":
        tid = f"t_{int(time.time() * 1000) % 0xffffffff:08x}"
        kf.add_card(db, tid, rest[0], status="todo", project_id=opts.get("--project"), body=opts.get("--body", ""),
                    created_ago=0)
        print(json.dumps({"id": tid}) if opts.get("--json") else tid)
    elif cmd == "block":
        kf.block(db, rest[0], rest[1], kind=opts.get("--kind"))
    elif cmd == "unblock":
        kf.set_status(db, rest[0], "ready")
    elif cmd == "comment":
        kf.comment(db, rest[0], opts.get("--author", "cli"), rest[1])
    elif cmd == "assign":
        with kf.conn(db) as c:
            c.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (rest[1], rest[0]))
    elif cmd == "edit":
        if "--priority" in opts:
            with kf.conn(db) as c:
                c.execute("UPDATE tasks SET priority = ? WHERE id = ?", (int(opts["--priority"]), rest[0]))
    elif cmd == "notify-unsubscribe":
        with kf.conn(db) as c:
            c.execute("DELETE FROM kanban_notify_subs WHERE task_id = ? AND platform = ?", (rest[0], opts["--platform"]))
    else:
        print(f"fake hermes: onbekend kanban-commando {cmd}", file=sys.stderr)
        return 2
    return 0


def main(argv):
    OWN.mkdir(parents=True, exist_ok=True)
    with open(OWN / "fake-hermes-calls.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": argv, "at": time.time()}, ensure_ascii=False) + "\n")
    if argv[:1] == ["cron"]:
        return cron(argv[1:])
    if argv[:1] == ["kanban"]:
        return kanban(argv[1:])
    if argv[:1] in (["chat"], ["sessions"]):
        return 0
    print(f"fake hermes: onbekend commando {argv[:2]}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
