#!/usr/bin/env python3
"""Discord REST helper for the team cron scripts (stdlib only; no gateway involved).

Channel IDs come from flow_config.channel_ids(): ~/.hermes/team/discord.json (written by discord_setup.py)
  {"guild": "...", "owner": "<owner user id>", "vragen": "<forum id>", "meldingen": "...",
   "staging": "...", "samenvatting": "..."}
overlaid with the IDs set in the local flow config (team/flow.yaml). Tag names come from discord.tags.
The bot token is DISCORD_BOT_TOKEN from ~/.hermes/.env. FLOW_DISCORD_API (e.g. http://127.0.0.1:8765/api/v10)
points every call at another base URL: the fake Discord of the clean-install test. Features the gateway's
`hermes send` lacks:
forum posts with a title, tags and buttons; silent messages (@silent); editing tags; archive + lock.

CLI (for the workflow side):  discord_post.py meldingen "text"   → ping message in #meldingen
                              discord_post.py staging "text" --silent
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flow_config as fc  # noqa: E402
from team_projects import HOME  # noqa: E402

API = os.environ.get("FLOW_DISCORD_API") or "https://discord.com/api/v10"
CHANNELS = fc.path("discord_ids")
SILENT = 1 << 12  # SUPPRESS_NOTIFICATIONS
STATUS_TAGS = fc.status_tags()  # tag names for open / beantwoord / verwerkt
MAX_CONTENT = 2000


def channels() -> dict:
    return fc.channel_ids()


def _token() -> str:
    try:
        env = (HOME / ".env").read_text(encoding="utf-8")
    except FileNotFoundError:
        if os.environ.get("FLOW_DISCORD_API"):
            return ""  # fake Discord: no token needed
        raise
    m = re.search(r"^DISCORD_BOT_TOKEN=(.*)$", env, re.M)
    return m.group(1).strip().strip("'\"") if m else ""


def api(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(3):
        req = urllib.request.Request(API + path, data=data, method=method, headers={
            "Authorization": "Bot " + _token(), "Content-Type": "application/json",
            "User-Agent": "DiscordBot (hermes-team-scripts, 1)"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(float(json.loads(e.read() or b"{}").get("retry_after", 2)) + 0.5)
                continue
            raise RuntimeError(f"Discord {method} {path}: HTTP {e.code} {e.read()[:300]!r}") from e
    return None


def _mentions(ping: bool) -> dict:
    owner = channels().get("owner", "")
    return {"parse": [], "users": [owner] if ping and owner else []}


def _content(text: str, ping: bool) -> str:
    owner = channels().get("owner", "")
    text = f"<@{owner}> {text}" if ping and owner else text
    return text if len(text) <= MAX_CONTENT else text[:MAX_CONTENT - 1] + "…"


def send(channel_id: str, text: str, *, ping=False, silent=False, components=None) -> dict:
    body = {"content": _content(text, ping), "allowed_mentions": _mentions(ping)}
    if silent:
        body["flags"] = SILENT
    if components:
        body["components"] = components
    return api("POST", f"/channels/{channel_id}/messages", body)


def ensure_tags(forum_id: str, names) -> dict:
    """``{lower name: tag id}`` for the forum, adding missing tags (Discord allows 20 per forum)."""
    forum = api("GET", f"/channels/{forum_id}")
    tags = forum.get("available_tags", [])
    have = {t["name"].lower() for t in tags}
    missing = [n for n in dict.fromkeys(names) if n and n.lower() not in have]
    if missing:
        tags = tags + [{"name": n[:20]} for n in missing]
        forum = api("PATCH", f"/channels/{forum_id}", {"available_tags": tags[:20]})
        tags = forum.get("available_tags", [])
    return {t["name"].lower(): t["id"] for t in tags}


def forum_post(forum_id: str, title: str, text: str, tag_ids, *, ping=False, components=None) -> dict:
    message = {"content": _content(text, ping), "allowed_mentions": _mentions(ping)}
    if components:
        message["components"] = components
    return api("POST", f"/channels/{forum_id}/threads",
               {"name": title[:100], "applied_tags": list(tag_ids)[:5], "message": message})


def set_status(thread_id: str, forum_id: str, status: str, *, close=False) -> None:
    """Swap the status tag (status key open/beantwoord/verwerkt → its configured tag name), keep the project tag;
    optionally archive + lock."""
    ids = ensure_tags(forum_id, STATUS_TAGS)
    status_ids = {ids[s.lower()] for s in STATUS_TAGS if s.lower() in ids}
    thread = api("GET", f"/channels/{thread_id}")
    md = thread.get("thread_metadata", {})
    keep = [t for t in thread.get("applied_tags", []) if t not in status_ids]
    tags = (keep + [ids[fc.tag(status).lower()]])[:5]
    if md.get("archived") and sorted(thread.get("applied_tags", [])) == sorted(tags) and (not close or md.get("locked")):
        return  # already in the wanted state (e.g. closed by an earlier run)
    if md.get("archived"):  # Discord refuses edits on an archived thread
        api("PATCH", f"/channels/{thread_id}", {"archived": False})
    body = {"applied_tags": tags}
    if close:
        body.update({"archived": True, "locked": True})
    api("PATCH", f"/channels/{thread_id}", body)


# ---------------------------------------------------------------- event keys
# One register for every Discord event the scripts post: key = "<kaart-id>:<soort>:<ronde>" (e.g.
# "t_ab12cd34:vraag:2051", "t_ab12cd34:staging", "t_ab12cd34:vastgelopen:blocked:1790000000").
# A key that is present is never posted again, whichever script or restart asks.
EVENTS = HOME / "state" / "discord-events.json"


EVENTS_LOCK = HOME / "state" / "discord-events.lock"


def _events() -> dict:
    """The register; {} only when it does not exist yet. An unreadable register raises instead of reading
    as empty: an empty read would make every script post everything again (audit S1)."""
    try:
        text = EVENTS.read_text()
    except FileNotFoundError:
        return {}
    try:
        return json.loads(text)
    except ValueError as exc:
        raise RuntimeError(f"{EVENTS} is onleesbaar; niets gepost (herstel uit back-up)") from exc


def _events_lock():
    """Exclusive lock around read-modify-write of the register, shared by every script (audit S1)."""
    import fcntl
    EVENTS_LOCK.parent.mkdir(parents=True, exist_ok=True)
    fh = open(EVENTS_LOCK, "w")
    fcntl.flock(fh, fcntl.LOCK_EX)
    return fh


def single_instance(name: str):
    """Exclusive lock for one script run (cron and a manual run must never overlap and post twice)."""
    import fcntl
    path = HOME / "state" / f"{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")
    fcntl.flock(fh, fcntl.LOCK_EX)  # waits for a running instance; the lock ends with the process
    return fh


def event_seen(key: str) -> bool:
    return key in _events()


def event_get(key: str):
    return _events().get(key)


def event_mark(key: str, **info) -> None:
    import os
    import time
    with _events_lock():  # released when the handle closes
        data = _events()
        data[key] = {**data.get(key, {}), **info, "at": data.get(key, {}).get("at", time.time())}
        EVENTS.parent.mkdir(parents=True, exist_ok=True)
        tmp = EVENTS.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0))
        tmp.replace(EVENTS)


def events_matching(prefix: str) -> dict:
    return {k: v for k, v in _events().items() if k.startswith(prefix)}


def edit(channel_id: str, message_id: str, text: str) -> None:
    api("PATCH", f"/channels/{channel_id}/messages/{message_id}",
        {"content": text[:MAX_CONTENT], "allowed_mentions": {"parse": []}})


def reopen(thread_id: str, forum_id: str) -> None:
    """Unarchive + unlock a post and tag it open again (a new question on the same card)."""
    api("PATCH", f"/channels/{thread_id}", {"archived": False, "locked": False})
    set_status(thread_id, forum_id, "open")


def thread_messages(thread_id: str, limit=50) -> list:
    return api("GET", f"/channels/{thread_id}/messages?limit={limit}") or []


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 2:
        sys.exit("gebruik: discord_post.py <meldingen|staging|samenvatting> <tekst> [--silent]")
    chan = channels()[args[0]]
    silent = "--silent" in sys.argv
    send(chan, args[1], ping=not silent and args[0] == "meldingen", silent=silent)


if __name__ == "__main__":
    main()
