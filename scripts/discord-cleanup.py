#!/usr/bin/env python3
"""Discord cleanup check (cron, --no-agent, every 10 min). Keeps the channels in step with the board:

1. #vragen: every post (open or archived) whose card is done or archived gets the tag "verwerkt" and is
   archived + locked, also when a later message reopened it. Posts of other cards are left alone.
2. Kanban notification subscriptions to Discord are removed: routine kanban messages ("👀 ready for review",
   "✔ done") stay out of Discord (kanban.auto_subscribe_on_create is off; this catches explicit ones).
   Failures reach #meldingen through board-guard.py.
3. #meldingen: a stuck-card message whose situation is over is edited to "✅ opgelost (HH:MM) — …"
   (without ping); a "kaart mislukt" message once the card is done or archived.
4. Bewaartermijnen (besluit eigenaar 03-10, flow-config opruimen.discord): berichten na hun termijn weg, ALTIJD eerst
   als JSON-regel in paden.discord_archief/<kanaal>/<jjjj-mm>.jsonl (map 700, bestand 600); pas na een geslaagde
   schrijfactie het DELETE. #chatlog/#staging/#samenvatting: alles na de termijn; #meldingen/#gezondheid: alleen
   "✅ opgelost" (termijn vanaf de bewerking); #workflow-inbox: alleen doorgestreepte; #vragen: hele posts met de tag
   verwerkt, termijn vanaf de archivering. Vastgepind blijft altijd. Een niet-opgeloste #meldingen-melding wordt nooit
   verwijderd; ouder dan melding_doorgeven_dagen gaat hij één keer naar de manager (event "melding-oud:<id>", dat
   board-guard.py als afwijking meeneemt in zijn wekker). Budget per ronde: max_per_ronde verwijderingen of
   max_seconden; de rest volgt de volgende ronde. Eén voor één (geen bulk-delete); een 429 met een lange
   retry_after stopt de ronde.
Uses the event register ~/.hermes/state/discord-events.json. ``--dry-run`` only reads (GET), writes nothing (no
archive, no event register, no lock) and prints per channel what would go.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discord_post as dp  # noqa: E402
import flow_config as fc  # noqa: E402
from flow_config import HOME, boards, kanban  # noqa: E402

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


def guard_module():
    spec = importlib.util.spec_from_file_location("board_guard", Path(__file__).with_name("board-guard.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def resolve_messages(dry):
    now = time.time()
    guard = guard_module()
    current = {c["episode"] for c in guard.stuck_cards(now) if not c.get("quiet")}
    guard_now = {f"guard:{a['id']}:{a['kind']}" for a in guard.anomalies(now)}
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


# ---------------------------------------------------------------- bewaartermijnen
DAG = 86400
DISCORD_EPOCH_MS = 1420070400000
OUD_EEN_VOOR_EEN = 14 * DAG  # Discord: ouder dan 14 dagen kan alleen één voor één weg
MAX_PAGINAS = 100  # per kanaal per ronde (10.000 berichten); de rest volgt de volgende ronde


def _prefix(dotted):
    return fc.text(dotted, tijd="\0", tekst="", wanneer="\0", oplossing="").split("\0")[0]


def snowflake(ts):
    return str(max(0, int(ts * 1000) - DISCORD_EPOCH_MS) << 22)


def ts_of(msg, field="timestamp"):
    value = msg.get(field)
    if value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return ((int(msg["id"]) >> 22) + DISCORD_EPOCH_MS) / 1000 if field == "timestamp" else None


def messages_before(channel, before, max_pages):
    """Alle berichten in ``channel`` ouder dan snowflake ``before`` (nieuwste eerst), 100 per GET."""
    pages = 0
    while pages < max_pages:
        page = dp.api("GET", f"/channels/{channel}/messages?limit=100&before={before}") or []
        pages += 1
        ids = [m["id"] for m in page if int(m["id"]) < int(before)]
        if not ids:
            return
        yield from (m for m in page if m["id"] in ids)
        before = min(ids, key=int)


def archive(key, msg, thread=None):
    """Eén JSON-regel per bericht in <archief>/<kanaal>/<jjjj-mm>.jsonl (map 700, bestand 600), met fsync.
    Een fout gaat omhoog: dan wordt er niets verwijderd."""
    root = fc.discord_archief()
    root.mkdir(mode=0o700, exist_ok=True)
    folder = root / key
    folder.mkdir(mode=0o700, exist_ok=True)
    when = ts_of(msg)
    rec = {"kanaal": key, "kanaal_id": msg.get("channel_id"), "bericht_id": msg["id"],
           "auteur_id": (msg.get("author") or {}).get("id"), "tijd": datetime.fromtimestamp(when, timezone.utc).isoformat(),
           "bewerkt": msg.get("edited_timestamp"), "inhoud": msg.get("content", ""),
           "thread_id": (thread or {}).get("id") or (msg.get("thread") or {}).get("id"),
           "thread_naam": (thread or {}).get("name") or (msg.get("thread") or {}).get("name"),
           "bijlagen": [a.get("url") for a in msg.get("attachments") or []], "gearchiveerd_op": time.time()}
    fd = os.open(folder / f"{datetime.fromtimestamp(when, timezone.utc):%Y-%m}.jsonl",
                 os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(rec, ensure_ascii=False) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def delete(path, wait):
    try:
        dp.api("DELETE", path, max_wait=wait)
    except dp.RateLimited:
        raise
    except RuntimeError as e:
        if "HTTP 404" not in str(e):  # al weg: telt als verwijderd
            raise


def candidates(key, channel, now, conf, limit=None, deadline=None):
    """``(weg, doorgeven)``: berichten van een tekstkanaal die weg mogen, en (alleen #meldingen) niet-opgeloste
    meldingen ouder dan de doorgeeftermijn. ``limit``/``deadline`` (echte ronde): stopt met lezen zodra er genoeg is
    of de tijd op is; de rest volgt de volgende ronde."""
    term = float(conf["termijnen_dagen"][key]) * DAG
    pass_on = float(conf.get("melding_doorgeven_dagen") or 14) * DAG
    opgelost, afgehandeld = _prefix("opgelost"), _prefix("workflow_inbox.afgehandeld")
    gone, report = [], []
    for m in messages_before(channel, snowflake(now - term), MAX_PAGINAS):
        if (limit is not None and len(gone) >= limit) or (deadline and time.monotonic() >= deadline):
            break
        if m.get("pinned") or now - ts_of(m) <= term:
            continue
        text = m.get("content") or ""
        if key in ("meldingen", "gezondheid"):
            if not text.startswith(opgelost):
                if key == "meldingen" and now - ts_of(m) > pass_on:
                    report.append(m)
                continue
            if now - (ts_of(m, "edited_timestamp") or ts_of(m)) <= term:
                continue  # termijn telt vanaf "opgelost"
        elif key == "workflow-inbox":
            if not (text.startswith("~~") and afgehandeld in text):
                continue
            if now - (ts_of(m, "edited_timestamp") or ts_of(m)) <= term:
                continue
        gone.append(m)
    return gone, report


def done_posts(forum, now, conf):
    """Posts in #vragen met de tag verwerkt, gearchiveerd en langer dan de termijn geleden gearchiveerd."""
    tags = {t["name"].lower(): t["id"] for t in dp.api("GET", f"/channels/{forum}")["available_tags"]}
    verwerkt = tags.get(fc.tag("verwerkt").lower())
    term = float(conf["termijnen_dagen"]["vragen"]) * DAG
    out = []
    for t in forum_posts(forum):
        md = t.get("thread_metadata") or {}
        if not verwerkt or verwerkt not in t.get("applied_tags", []) or not md.get("archived") or t.get("flags", 0) & 2:
            continue  # flags & 2 = vastgepinde post
        if now - ts_of({"id": t["id"], "timestamp": md.get("archive_timestamp")}) > term:
            out.append(t)
    return out


def retention(dry, now=None):
    """Bewaartermijnen. Droog: telt per kanaal. Anders: archiveren + verwijderen binnen het rondebudget."""
    now = now or time.time()
    conf = fc.get("opruimen.discord") or {}
    terms = conf.get("termijnen_dagen") or {}
    budget, deadline = int(conf.get("max_per_ronde", 50)), time.monotonic() + float(conf.get("max_seconden", 60))
    ids, out, doorgegeven = dp.channels(), {}, []
    stop = None

    def over_budget():
        return budget <= 0 or time.monotonic() >= deadline

    for key in sorted(terms, key=lambda k: k == "chatlog"):  # #chatlog (de grootste) als laatste: de rest wacht niet
        if key not in ids or (not dry and (stop or over_budget())):
            continue
        try:
            if key == "vragen":
                posts = done_posts(ids[key], now, conf)
                stamps = [ts_of({"id": t["id"], "timestamp": t["thread_metadata"].get("archive_timestamp")})
                          for t in posts]
                gone, report = posts, []
            else:
                gone, report = candidates(key, ids[key], now, conf, *((None, None) if dry else (budget, deadline)))
                stamps = [ts_of(m) for m in gone]
            if dry:
                row = {"weg": len(gone), "ouder_dan_14d": sum(now - x > OUD_EEN_VOOR_EEN for x in stamps)}
                if stamps:
                    row.update(oudste=fc.tijd(min(stamps), "%d-%m-%Y"), nieuwste=fc.tijd(max(stamps), "%d-%m-%Y"))
                if key == "meldingen":
                    row.update(opgelost_weg=len(gone), niet_opgelost_naar_manager=len(report))
                out[key] = row
                continue
            for m in report:
                ev = f"melding-oud:{m['id']}"
                if not dp.event_seen(ev):
                    card = CARD_RE.search(m.get("content") or "")
                    dp.event_mark(ev, kanaal=ids[key], bericht=m["id"], card=card.group(0) if card else "",
                                  tekst=re.sub(r"^<@\d+>\s*", "", m.get("content") or "")[:300],
                                  sinds=fc.tijd(ts_of(m), "%d-%m-%Y"))
                    doorgegeven.append(m["id"])
            n = 0
            for item in gone:
                if over_budget():
                    break
                wait = max(1.0, deadline - time.monotonic())
                if key == "vragen":
                    msgs = list(messages_before(item["id"], snowflake(now + DAG), 1000))
                    for m in msgs:
                        archive(key, m, thread=item)
                    delete(f"/channels/{item['id']}", wait)
                else:
                    archive(key, item)
                    delete(f"/channels/{ids[key]}/messages/{item['id']}", wait)
                budget -= 1
                n += 1
            if n:
                out[key] = n
        except dp.RateLimited as e:
            stop = f"429: {e}"
        except (OSError, RuntimeError, KeyError, ValueError) as e:
            out[key] = f"FOUT {type(e).__name__}: {str(e)[:200]}"
            if isinstance(e, OSError):
                stop = "archief niet schrijfbaar; niets meer verwijderd"
    if stop and not dry:
        out["gestopt"] = stop
    return out, doorgegeven


def main():
    dry = "--dry-run" in sys.argv
    _lock = None if dry else dp.single_instance("discord-cleanup")  # noqa: F841 (held until exit; droog: geen lock)
    if "vragen" not in dp.channels():
        print("geen #vragen-kanaal ingesteld (draai eerst discord_setup.py); niets te controleren")
        return
    forum = dp.channels()["vragen"]
    report = {"posts_gesloten": close_done_posts(forum, dry), "abonnementen_afgemeld": drop_discord_subscriptions(dry),
              "meldingen_opgelost": resolve_messages(dry)}
    report["bewaartermijnen"], report["meldingen_doorgegeven"] = retention(dry)
    if dry:
        print({k: v for k, v in report.items() if k != "bewaartermijnen"})
        for key, row in report["bewaartermijnen"].items():
            print(f"bewaartermijn #{key}: {row}")
    elif any(report.values()):
        print(report)


if __name__ == "__main__":
    main()
