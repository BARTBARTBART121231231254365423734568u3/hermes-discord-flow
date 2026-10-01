#!/usr/bin/env python3
"""A tiny fake of the Discord REST API (v10) for the clean-install test: no network, no token.

Start: fake_discord.py --port-file <file> --log <calls.jsonl> [--guild <id>]
The scripts reach it through FLOW_DISCORD_API=http://127.0.0.1:<port>/api/v10. It keeps one guild with channels,
forum threads (tags, archived/locked) and messages (content, components, flags) in memory, and writes every call
(method, path, body) as one JSON line to the log. IDs are small numbers (no real snowflakes).

Test endpoints (not Discord):
  GET  /_test/state            the whole state as JSON
  POST /_test/message          {"channel", "author", "content"}: a message written by a user (e.g. typed in a post)
"""
import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LOCK = threading.Lock()
STATE = {"next": 1000, "bot": "999", "guild": None, "channels": {}, "messages": {}}
LOG = None


def new_id():
    STATE["next"] += 1
    return str(STATE["next"])


def err(code, dc_code, msg):
    return code, {"code": dc_code, "message": msg}


def channel_list():
    return [c for c in STATE["channels"].values() if c.get("guild_id") == STATE["guild"]["id"] and c["type"] != 11]


def tags_with_ids(tags):
    out = []
    for t in tags:
        t = dict(t)
        t.setdefault("id", new_id())
        t.setdefault("moderated", False)
        out.append(t)
    return out


def make_message(channel_id, body, author=None):
    msg = {"id": new_id(), "channel_id": channel_id, "content": body.get("content", ""),
           "components": body.get("components", []), "flags": body.get("flags", 0),
           "allowed_mentions": body.get("allowed_mentions"), "author": {"id": author or STATE["bot"], "bot": not author}}
    STATE["messages"].setdefault(channel_id, []).append(msg)
    return msg


def route(method, path, query, body):  # noqa: C901 (one small router)
    g = STATE["guild"]
    m = re.fullmatch(r"/api/v10(/.*)", path)
    if not m:
        return route_test(method, path, body)
    p = m.group(1)
    if p == "/users/@me" and method == "GET":
        return 200, {"id": STATE["bot"], "username": "flow-bot"}
    m = re.fullmatch(r"/guilds/(\w+)(/.*)?", p)
    if m:
        if m.group(1) != g["id"]:
            return err(404, 10004, "Unknown Guild")
        rest = m.group(2) or ""
        if rest == "" and method == "GET":
            return 200, g
        if rest == "" and method == "PATCH":
            g.update(body)
            return 200, g
        if rest == "/channels" and method == "GET":
            return 200, channel_list()
        if rest == "/channels" and method == "POST":
            c = {"id": new_id(), "guild_id": g["id"], "name": body["name"], "type": body.get("type", 0),
                 "parent_id": body.get("parent_id"), "topic": body.get("topic"), "position": len(channel_list()),
                 "permission_overwrites": body.get("permission_overwrites", []),
                 "available_tags": tags_with_ids(body.get("available_tags", []))}
            STATE["channels"][c["id"]] = c
            return 201, c
        if rest == "/channels" and method == "PATCH":
            for item in body:
                STATE["channels"][item["id"]]["position"] = item["position"]
            return 204, None
        if rest == "/threads/active" and method == "GET":
            return 200, {"threads": [c for c in STATE["channels"].values()
                                     if c["type"] == 11 and not c["thread_metadata"]["archived"]]}
    m = re.fullmatch(r"/channels/(\w+)(/.*)?", p)
    if m:
        cid, rest = m.group(1), m.group(2) or ""
        c = STATE["channels"].get(cid)
        if c is None:
            return err(404, 10003, "Unknown Channel")
        if rest == "" and method == "GET":
            return 200, c
        if rest == "" and method == "PATCH":
            if c["type"] == 11:
                md = c["thread_metadata"]
                if md["locked"] and body.get("locked") is not False and set(body) - {"archived", "locked"}:
                    return err(403, 50083, "Thread is locked")
                for k in ("archived", "locked"):
                    if k in body:
                        md[k] = bool(body[k])
                if "applied_tags" in body:
                    c["applied_tags"] = list(body["applied_tags"])
                if "name" in body:
                    c["name"] = body["name"]
            else:
                if "available_tags" in body:
                    body = {**body, "available_tags": tags_with_ids(body["available_tags"])}
                c.update(body)
            return 200, c
        if rest == "/threads" and method == "POST":
            if c["type"] != 15:
                return err(400, 50024, "Cannot execute action on this channel type")
            t = {"id": new_id(), "guild_id": g["id"], "parent_id": cid, "type": 11, "name": body["name"],
                 "applied_tags": list(body.get("applied_tags", [])),
                 "thread_metadata": {"archived": False, "locked": False, "archive_timestamp": "2026-01-01T00:00:00"}}
            STATE["channels"][t["id"]] = t
            first = make_message(t["id"], body.get("message", {}))
            STATE["messages"][t["id"]][-1]["id"] = t["id"]  # the starter message has the thread's id
            first["id"] = t["id"]
            return 201, {**t, "message": first}
        if rest.startswith("/threads/archived/public") and method == "GET":
            return 200, {"threads": [x for x in STATE["channels"].values() if x["type"] == 11 and x["parent_id"] == cid
                                     and x["thread_metadata"]["archived"]], "has_more": False}
        if rest == "/messages" and method == "POST":
            if c["type"] == 11:
                if c["thread_metadata"]["locked"]:
                    return err(403, 50083, "Thread is locked")
                c["thread_metadata"]["archived"] = False  # a new message unarchives a post
            if c["type"] == 15:
                return err(400, 50024, "Cannot send messages in a forum channel")
            return 200, make_message(cid, body)
        if rest == "/messages" and method == "GET":
            limit = int(query.get("limit", ["50"])[0])
            return 200, list(reversed(STATE["messages"].get(cid, [])))[:limit]
        m3 = re.fullmatch(r"/messages/(\w+)/reactions/([^/]+)/@me", rest)
        if m3 and method == "PUT":
            msg = next((x for x in STATE["messages"].get(cid, []) if x["id"] == m3.group(1)), None)
            if msg is None:
                return err(404, 10008, "Unknown Message")
            msg.setdefault("reactions", []).append(m3.group(2))
            return 204, None
        m2 = re.fullmatch(r"/messages/(\w+)", rest)
        if m2:
            msg = next((x for x in STATE["messages"].get(cid, []) if x["id"] == m2.group(1)), None)
            if msg is None:
                return err(404, 10008, "Unknown Message")
            if method == "GET":
                return 200, msg
            if method == "PATCH":
                for k in ("content", "components", "flags"):
                    if k in body:
                        msg[k] = body[k]
                msg["edited"] = True
                return 200, msg
    return err(404, 0, f"fake Discord kent {method} {p} niet")


def route_test(method, path, body):
    if path == "/health" and method == "GET":
        return 200, {"ok": True}  # a staging health endpoint for the checks
    if path == "/_test/state":
        return 200, STATE
    if path == "/_test/message" and method == "POST":
        return 200, make_message(body["channel"], {"content": body["content"]}, author=body["author"])
    return err(404, 0, "onbekend testpad")


class Handler(BaseHTTPRequestHandler):
    def _do(self, method):
        url = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        body = json.loads(raw) if raw else None
        with LOCK:
            code, data = route(method, url.path, parse_qs(url.query), body if body is not None else {})
            if not url.path.startswith("/_test/"):
                with open(LOG, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"method": method, "path": url.path + (f"?{url.query}" if url.query else ""),
                                         "body": body, "status": code,
                                         "auth": bool(self.headers.get("Authorization"))}, ensure_ascii=False) + "\n")
        out = json.dumps(data).encode() if data is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        self._do("GET")

    def do_POST(self):
        self._do("POST")

    def do_PATCH(self):
        self._do("PATCH")

    def do_PUT(self):
        self._do("PUT")

    def do_DELETE(self):
        self._do("DELETE")

    def log_message(self, *_a):
        pass


def main():
    global LOG
    ap = argparse.ArgumentParser()
    ap.add_argument("--port-file", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--guild", default="100")
    a = ap.parse_args()
    LOG = a.log
    STATE["guild"] = {"id": a.guild, "name": "Testserver", "features": [], "verification_level": 0}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    with open(a.port_file, "w") as fh:
        fh.write(str(srv.server_address[1]))
    srv.serve_forever()


if __name__ == "__main__":
    main()
