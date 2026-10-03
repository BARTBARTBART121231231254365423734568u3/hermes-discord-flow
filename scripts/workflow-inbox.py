#!/usr/bin/env python3
"""workflow-inbox.py sync [--dry-run] | afhandelen <bestand> "<oplossing>" | status

The workflow inbox (flow config paden.workflow_inbox, one .md file per item, format in LEESMIJ.md) mirrored in
Discord WORKFLOW → #workflow-inbox: one silent bot message per item with a plain title, 2–3 sentences, project,
reporter, date, file name and "Status: open". Channel (discord.kanalen.workflow-inbox) and all texts
(teksten.workflow_inbox) come from the flow config. When an item is handled the SAME message is
edited: title and text struck through (~~ ~~), "✅ Afgehandeld <datum tijd>: <oplossing>" under it, a ✅ reaction,
and the file moves to afgehandeld/.

State lives in the file itself, as YAML frontmatter the script adds:
  status: open | afgehandeld        discord_message_id: <id>       discord_status: open | afgehandeld
  gepost_op / afgehandeld_op: ISO    oplossing: <text>
so a rerun never posts twice. Files the manager is still writing (changed < 60 s ago) are skipped until the next
run. Older handled files without frontmatter (moved by hand with an "Afgehandeld: <datum> — <wat>" line) are
posted struck through once, so the history is complete.

Secrets: every message goes through redact(): no credentials in URLs, no token-like strings, no value of any .env.
If redaction had to remove anything, the message shrinks to the title and the file name only.

Runs after every health check (ExecStartPost of hermes-health.service, every 30 min) and by hand.
``afhandelen`` sets the frontmatter and syncs that item right away (the ops side uses it instead of moving files).
"""
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discord_post as dp  # noqa: E402
import flow_config as fc  # noqa: E402
from flow_config import HOME  # noqa: E402  (.env files of this install)

NL = fc.tz()
INBOX = Path(os.environ.get("HERMES_WORKFLOW_INBOX") or fc.path("workflow_inbox"))
DONE = INBOX / "afgehandeld"
CHANNEL_KEY = "workflow-inbox"
SETTLE = 60  # seconds a file must be unchanged before it is touched (the manager may still be writing)
FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)
NAME = re.compile(r"^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})-")
SECRETISH = [
    re.compile(r"://[^/\s:@]+:(?!\*\*\*@|\$)[^@/\s]+@"),          # credentials in a URL (not $VAR or ***)
    re.compile(r"\b(?:ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{20,}"),
    re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\b[MNO][A-Za-z\d]{23,25}\.[\w-]{6}\.[\w-]{27,}"),  # Discord bot token shape
    re.compile(r"(?i)\b(?:password|passwd|wachtwoord|token|secret|api[_-]?key)\s*[:=]\s*['\"]?[^\s'\"*]{8,}"),
    re.compile(r"(?<![A-Za-z0-9+/_-])(?=[A-Za-z0-9+/]*\d)(?=[A-Za-z0-9+/]*[A-Za-z])[A-Za-z0-9+/]{32,}={0,2}"),  # opaque blobs (no hyphenated words)
]


def env_values():
    vals = set()
    for f in [HOME / ".env", *HOME.glob("profiles/*/.env")]:
        try:
            for line in f.read_text(errors="replace").splitlines():
                m = re.match(r"^\s*[A-Za-z_][A-Za-z0-9_]*\s*=\s*(.+)$", line)
                if m:
                    v = m.group(1).strip().strip("'\"")
                    if len(v) >= 8 and not re.fullmatch(r"[\d,.]+|true|false|https?://[^@\s]+|/\S+", v):
                        vals.add(v)
        except OSError:
            pass
    return vals


def redact(text: str, secrets=None):
    """(clean text, True when something secret-like was removed)."""
    hit = False
    for s in secrets if secrets is not None else env_values():
        if s in text:
            text, hit = text.replace(s, "***"), True
    for rx in SECRETISH:
        new = rx.sub("***", text)
        hit, text = hit or new != text, new
    return text, hit


def split(text: str):
    """(frontmatter dict, body)."""
    m = FRONT.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        if k.strip():
            try:
                meta[k.strip()] = json.loads(v.strip()) if v.strip() else ""
            except ValueError:
                meta[k.strip()] = v.strip()
    return meta, text[m.end():]


def join(meta: dict, body: str) -> str:
    lines = [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in meta.items()]
    return "---\n" + "\n".join(lines) + "\n---\n" + body


def field(body: str, name: str) -> str:
    m = re.search(rf"^{name}:\s*(.+?)(?=^\S[^:\n]{{0,30}}:|\Z)", body, re.M | re.S)
    return " ".join(m.group(1).split()) if m else ""


def sentences(text: str, n=3, limit=420) -> str:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(`])", text)
    out = ""
    for p in parts[:n]:
        if len(out) + len(p) > limit and out:
            break
        out = (out + " " + p).strip()
    return out[:limit]


def stamp(path: Path) -> str:
    m = NAME.match(path.name)
    return f"{m.group(3)}-{m.group(2)} {m.group(4)}:{m.group(5)}" if m else "?"


def project_of(body: str) -> str:
    """The first project slug from the flow config named in the item, else teksten.workflow_inbox.geen_project."""
    try:
        slugs = [p.get("slug") for p in fc.load().get("projecten") or [] if p.get("slug")]
    except Exception:  # noqa: BLE001
        slugs = []
    for slug in slugs:
        if re.search(rf"\b{re.escape(slug)}\b", body, re.I):
            return slug
    return fc.text("workflow_inbox.geen_project")


def solution_from_body(body: str) -> str:
    found = re.findall(r"^Afgehandeld:\s*(?:\d{4}-\d{2}-\d{2}\s*[—–-]\s*)?(.+)$", body, re.M)
    return found[-1].strip() if found else ""


def render(path: Path, meta: dict, body: str, secrets=None) -> str:
    title = next((l[2:].strip() for l in body.splitlines() if l.startswith("# ")), path.stem)
    what = field(body, "Wat") or field(body, "Gevraagd")
    who = field(body, "Wie vroeg het") or "?"
    summary = sentences(what)
    lines = [fc.text("workflow_inbox.kop", titel=title), summary,
             fc.text("workflow_inbox.regel", project=project_of(body), wie=who[:80], datum=stamp(path)), f"`{path.name}`"]
    text, hit = redact("\n".join(l for l in lines if l), secrets)
    if hit:  # doubt → title and file name only
        clean_title, _ = redact(title, secrets)
        text = "\n".join([fc.text("workflow_inbox.kop", titel=clean_title), fc.text("workflow_inbox.verborgen"),
                          f"`{path.name}`"])
    if meta.get("status") == "afgehandeld":
        struck = "\n".join(f"~~{l}~~" if l and not l.startswith("`") else l for l in text.splitlines())
        when = meta.get("afgehandeld_op") or ""
        try:
            when = datetime.fromisoformat(when).astimezone(NL).strftime("%d-%m %H:%M")
        except ValueError:
            pass
        sol, _ = redact(meta.get("oplossing") or fc.text("workflow_inbox.geen_oplossing"), secrets)
        return f"{struck}\n" + fc.text("workflow_inbox.afgehandeld", wanneer=when, oplossing=sol[:600])
    return text + "\n" + fc.text("workflow_inbox.open")


def settled(path: Path) -> bool:
    return time.time() - path.stat().st_mtime >= SETTLE


def channel():
    ch = dp.channels().get(CHANNEL_KEY)
    if not ch:
        raise SystemExit("kanaal #workflow-inbox ontbreekt in team/discord.json (discord_setup.py draaien)")
    return ch


def react(ch, mid):
    try:
        dp.api("PUT", f"/channels/{ch}/messages/{mid}/reactions/%E2%9C%85/@me")
    except RuntimeError as exc:
        print(f"reactie mislukt: {exc}", file=sys.stderr)


def sync_file(path: Path, dry=False, secrets=None) -> str:
    """Bring one file in line with Discord; returns what happened ('' = nothing)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    meta, body = split(text)
    in_done = path.parent == DONE
    if in_done and meta.get("status") != "afgehandeld":  # moved by hand (old way): handled
        meta.update(status="afgehandeld", afgehandeld_op=meta.get("afgehandeld_op") or datetime.fromtimestamp(
            path.stat().st_mtime, NL).isoformat(timespec="minutes"), oplossing=meta.get("oplossing") or solution_from_body(body))
    meta.setdefault("status", "open")
    done = meta["status"] == "afgehandeld"
    if meta.get("discord_message_id") and meta.get("discord_status") == meta["status"] and (done == in_done):
        return ""
    content = render(path, meta, body, secrets)
    if dry:
        return f"zou {'doorstrepen' if done else 'plaatsen'}: {path.name}"
    ch = channel()
    action = ""
    if not meta.get("discord_message_id"):
        msg = dp.send(ch, content, silent=True)
        meta.update(discord_message_id=msg["id"], gepost_op=datetime.now(NL).isoformat(timespec="minutes"))
        action = "geplaatst" + (" (doorgestreept)" if done else "")
    elif meta.get("discord_status") != meta["status"]:
        dp.edit(ch, meta["discord_message_id"], content)
        action = "doorgestreept" if done else "bijgewerkt"
    if done and meta.get("discord_status") != "afgehandeld":
        react(ch, meta["discord_message_id"])
    meta["discord_status"] = meta["status"]
    new = join(meta, body)
    if done and not in_done:
        DONE.mkdir(exist_ok=True)
        target = DONE / path.name
        target.write_text(new, encoding="utf-8")
        path.unlink()
        action += ", verplaatst naar afgehandeld/"
    else:
        path.write_text(new, encoding="utf-8")
    return f"{action}: {path.name}"


def items():
    open_ = sorted(p for p in INBOX.glob("*.md") if p.name != "LEESMIJ.md")
    done = sorted(DONE.glob("*.md")) if DONE.exists() else []
    return open_, done


def sync(dry=False):
    if not dry:
        _lock = dp.single_instance("workflow-inbox")  # noqa: F841 (held until exit)
    secrets = env_values()
    out = []
    open_, done = items()
    for p in done + open_:  # history first, so the channel reads in order
        if not settled(p):
            continue
        try:
            r = sync_file(p, dry, secrets)
        except Exception as exc:  # noqa: BLE001 (one bad file never blocks the rest)
            r = f"FOUT {p.name}: {type(exc).__name__}: {str(exc)[:160]}"
        if r:
            out.append(r)
    return out


def summary_line():
    """For the morning report (teksten.ochtendrapport.inbox_regel / inbox_leeg)."""
    open_, _ = items()
    if not open_:
        return fc.text("ochtendrapport.inbox_leeg")
    return fc.text("ochtendrapport.inbox_regel", aantal=len(open_), oudste=stamp(open_[0]))


def handle(name: str, solution: str):
    path = INBOX / Path(name).name
    if not path.exists():
        raise SystemExit(f"{path} bestaat niet (open punten staan in {INBOX})")
    meta, body = split(path.read_text(encoding="utf-8", errors="replace"))
    meta.update(status="afgehandeld", afgehandeld_op=datetime.now(NL).isoformat(timespec="minutes"),
                oplossing=" ".join(solution.split()))
    tekst = join(meta, body)
    # Altijd via een tijdelijk bestand + os.replace: een punt van de manager (de agent) mag de ops-gebruiker niet
    # altijd beschrijven (bv. 644), en een eigen tijd zetten mag alleen de eigenaar. Daarna is het een ops-bestand
    # (workflow-inbox staat op de lijst van probe_eigenaar in hermes-health.py).
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(tekst, encoding="utf-8")
    os.utime(tmp, (time.time() - SETTLE, time.time() - SETTLE))
    os.replace(tmp, path)
    _lock = dp.single_instance("workflow-inbox")  # noqa: F841
    return sync_file(path, secrets=env_values())


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "sync"
    if cmd == "sync":
        for line in sync(dry="--dry-run" in args):
            print(line)
    elif cmd == "afhandelen" and len(args) >= 3:
        print(handle(args[1], " ".join(args[2:])))
    elif cmd == "status":
        print(summary_line())
    else:
        raise SystemExit(__doc__.splitlines()[0])


if __name__ == "__main__":
    main()
