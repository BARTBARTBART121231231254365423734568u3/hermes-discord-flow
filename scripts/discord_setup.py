#!/usr/bin/env python3
"""Discord layout for the Hermes team, idempotent: builds it from scratch or repairs it.

Layout (from the flow config: discord.categorieen + discord.kanalen; the default, top to bottom):
  GENERAL        #chatlog                    conversations owner ↔ manager (gateway home channel)
  HERMES AGENTS  #vragen (forum, ping)       one post per question, buttons, tags open/beantwoord/verwerkt + project
                 #meldingen (ping)           stuck or failed cards, phase complete, gateway down/unexpected restart
                 #staging (silent)           one line per card on staging
                 #samenvatting (silent)      daily summary 08:00
  WORKFLOW       #ochtendrapport (1 ping/day) morning report of the workflow side; on Monday the weekly incident overview
                 #gezondheid (silent)        health findings that do not stop project work; edited to "✅ opgelost"
                 #releases (silent)          one line per workflow release
  Systeem        #regels, #moderator-updates required for a Community server (collapse it in the app)

Rights: in channels with ``alleen_bot: true`` only the bot posts (@everyone: no send); in a forum with
``alleen_bot`` only the bot starts posts, everyone may reply inside a post. Community is switched on (required
for forums) unless discord.community is false.
Guild and owner come from flow_config.channel_ids() (discord.guild_id / discord.owner_id or team/discord.json);
the channel IDs found or created are written back to team/discord.json under the channel key (vragen,
meldingen, …). Existing channels (same name + type) are reused, never recreated. ``--dry-run`` only prints.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discord_post as dp  # noqa: E402
import flow_config as fc  # noqa: E402
from flow_config import active_projects  # noqa: E402

TEXT, CATEGORY, FORUM = 0, 4, 15
SEND_MESSAGES = 1 << 11  # in a forum: start a post
SEND_IN_THREADS = 1 << 38
STATUS_TAGS = list(fc.status_tags())


def layout():
    """``[(category name, [(key, name, kind, topic, rights)])]`` from the config, in config order."""
    d = fc.get("discord")
    out = []
    for cat_key, cat_name in d["categorieen"].items():
        rows = []
        for key, ch in d["kanalen"].items():
            if ch.get("categorie") != cat_key:
                continue
            kind = FORUM if ch.get("soort") == "forum" else TEXT
            rights = None
            if ch.get("alleen_bot"):
                rights = {"deny": SEND_MESSAGES, "allow": SEND_IN_THREADS} if kind == FORUM else {"deny": SEND_MESSAGES}
            rows.append((key, ch.get("naam") or key, kind, str(ch.get("onderwerp") or "").format(eigenaar=fc.owner_name()),
                         rights))
        if rows:
            out.append((cat_name, rows))
    return out


def main():
    dry = "--dry-run" in sys.argv
    cfg = dp.channels()
    if not cfg.get("guild") or not cfg.get("owner"):
        sys.exit("discord.guild_id en discord.owner_id ontbreken (flow-config of team/discord.json)")
    guild_id, owner = cfg["guild"], cfg["owner"]
    me = dp.api("GET", "/users/@me")["id"]
    chans = dp.api("GET", f"/guilds/{guild_id}/channels")

    def find(name, kind):
        return next((c for c in chans if c["name"].lower() == name.lower() and c["type"] == kind), None)

    def create(body):
        print("aanmaken:", body["name"])
        if dry:
            return {"id": f"<{body['name']}>", **body}
        c = dp.api("POST", f"/guilds/{guild_id}/channels", body)
        chans.append(c)
        return c

    ids = {"guild": guild_id, "owner": owner}
    cat_ids = []
    for cat_name, channels in layout():
        cat = find(cat_name, CATEGORY) or create({"name": cat_name, "type": CATEGORY})
        cat_ids.append(cat["id"])
        for key, name, kind, topic, rights in channels:
            c = find(name, kind) or create({"name": name, "type": kind, "parent_id": cat["id"], "topic": topic})
            ids[key] = c["id"]
            patch = {}
            if c.get("parent_id") != cat["id"]:
                patch["parent_id"] = cat["id"]
            if kind == FORUM:
                have = {t["name"].lower() for t in c.get("available_tags", [])}
                want = STATUS_TAGS + [p["name"][:20] for p in active_projects().values()]
                missing = [t for t in want if t.lower() not in have]
                if missing:
                    patch["available_tags"] = (c.get("available_tags", []) + [{"name": t} for t in missing])[:20]
            if rights:
                keep = [o for o in c.get("permission_overwrites", []) if o["id"] not in (guild_id, me)]
                want_ow = [{"id": guild_id, "type": 0, "allow": str(rights.get("allow", 0)), "deny": str(rights["deny"])},
                           {"id": me, "type": 1, "allow": str(SEND_MESSAGES | SEND_IN_THREADS), "deny": "0"}]
                current = sorted((o["id"], str(o["allow"]), str(o["deny"])) for o in c.get("permission_overwrites", []))
                if current != sorted((o["id"], o["allow"], o["deny"]) for o in keep + want_ow):
                    patch["permission_overwrites"] = keep + want_ow
            if patch:
                print(f"bijwerken #{name}:", sorted(patch))
                if not dry:
                    dp.api("PATCH", f"/channels/{c['id']}", patch)

    guild = dp.api("GET", f"/guilds/{guild_id}")
    if fc.get("discord.community", True) and "COMMUNITY" not in guild.get("features", []):
        body = {"features": sorted(set(guild.get("features", [])) | {"COMMUNITY"}),
                "verification_level": max(1, guild.get("verification_level", 0)), "explicit_content_filter": 2,
                "rules_channel_id": ids["regels"], "public_updates_channel_id": ids["moderator-updates"]}
        print("Community aanzetten")
        if not dry:
            dp.api("PATCH", f"/guilds/{guild_id}", body)

    cats = sorted([c for c in chans if c["type"] == CATEGORY], key=lambda c: c["position"])
    order = [c for c in cats if c["id"] not in cat_ids] + [c for i in cat_ids for c in cats if c["id"] == i]
    if [c["id"] for c in cats] != [c["id"] for c in order]:
        print("categorievolgorde:", [c["name"] for c in order])
        if not dry:
            dp.api("PATCH", f"/guilds/{guild_id}/channels", [{"id": c["id"], "position": i} for i, c in enumerate(order)])

    print("discord.json:", ids)
    if not dry:
        dp.CHANNELS.parent.mkdir(parents=True, exist_ok=True)
        dp.CHANNELS.write_text(json.dumps(ids, indent=2) + "\n")


if __name__ == "__main__":
    main()
