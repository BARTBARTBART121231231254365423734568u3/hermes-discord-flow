#!/usr/bin/env python3
"""Clean-install test of Hermes Discord Flow, with a fake Discord and a fake ``hermes``/``systemctl``.

    python3 tests/clean_install_test.py [--bewaar] [--hermes <checkout> --pytest-python <python> [--pythonpath <p>]]

Builds a fresh temp dir with ONLY a copy of this repo; HOME and HERMES_HOME point into it, so nothing of the
person running it is read or touched. ``install.sh`` puts config.example.yaml on the config path; the only extra
input is what a new user types: the guild and owner ID (fake numbers) in team/discord.json. Then it walks the
README test list and checks every Discord call in the fake's log:

  install        install.sh --dry-run changes nothing; install.sh --basis --geen-cron; config check
  setup          discord_setup.py creates categories, channels, the forum with tags, rights and Community;
                 a second run creates nothing
  cron           install.sh creates the Hermes cron jobs (samenvatting to #samenvatting); a second run none
  vraag          a valid question → forum post with tags, ⭐ button first, "Anders…"; an incomplete one goes back
  knop           simulated click (what the patched gateway does) → beantwoord + archived (not locked); card done → the archived post stays untouched
  anders         simulated "Anders…" answer + "Wacht op …" wait state → beantwoord, no new post; team-status
  melding        discord_post.py meldingen with ping; board-guard.py: a block wakes the manager (no message), a
                 failed card → one "kaart mislukt" with ping, never twice, resolved later
  staging        first run summary, then one silent line per card; phase complete → #meldingen + production question
  samenvatting   daily-summary-data.py output + the cron job's delivery target and prompt
  ochtendrapport the morning report in #ochtendrapport (one ping)
  gezondheid     hermes-health.py: blocking failure → #meldingen, resolved → "✅ opgelost", no duplicates
  release        hermes-release.py → flow-controle --quick green → one silent line in #releases
  bordbewaking   board-guard.py on an empty scratch board: 0 anomalies, nothing posted
  opruiming      werkmap_groei.py --dry-run and the growth report (--groei)
  gateway        gateway-watch.py: baseline, then an unexpected restart → #meldingen
  privacy        the scanner finds nothing in the repo copy nor in what was posted; no real home path anywhere
  patches        (with --hermes) the button/modal tests of the patches in the given Hermes checkout

Exit 0 when every step passes. ``--bewaar`` keeps the temp dir (its path is printed).
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request
from pathlib import Path

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
sys.path.insert(0, str(TESTS))
import kanban_fixture as kf  # noqa: E402

GUILD, OWNER, OTHER = "100", "4242", "5151"
VALID = """{prefix}
Wat speelt er: we kiezen de kleur van de bevestigknop.
Vraag: Welke kleur krijgt de knop?
Optie 1 ⭐ Aanbevolen: Groen
  Inhoud: de knop wordt groen.
  Voordeel: valt goed op.
  Nadeel: past minder bij het logo.
  Daarna: de bouwer past de kleur aan.
Optie 2: Blauw
  Inhoud: de knop wordt blauw.
  Voordeel: past bij het logo.
  Nadeel: valt minder op.
  Daarna: de bouwer past de kleur aan.
Waarom deze aanbeveling: 1) groen valt het meest op; 2) het is de gewone kleur voor een bevestiging. Kies liever optie 2 als de huisstijl zwaarder weegt.
Zonder antwoord: de knop blijft zoals hij is.
Details: alleen een test."""


class Run:
    def __init__(self, keep):
        self.tmp = Path(tempfile.mkdtemp(prefix="flow-schoon-"))
        self.keep = keep
        self.home = self.tmp / "home"
        self.hh = self.home / ".hermes"
        self.repo = self.tmp / "repo"
        self.bin = self.tmp / "bin"
        self.results = []
        self.real_home = str(Path.home())

    # ------------------------------------------------------------------ setup
    def prepare(self):
        shutil.copytree(REPO, self.repo, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
        self.home.mkdir(parents=True)
        self.bin.mkdir()
        for name, target in (("hermes", "fake_hermes.py"), ("systemctl", "fake_systemctl.py")):
            exe = self.bin / name
            exe.write_text(f"#!/bin/sh\nexec {sys.executable} {self.repo / 'tests' / target} \"$@\"\n")
            exe.chmod(0o755)
        (self.home / ".local" / "bin").mkdir(parents=True)
        shutil.copy2(self.bin / "hermes", self.home / ".local" / "bin" / "hermes")  # = paden.hermes_bin
        self.calls = self.tmp / "discord-calls.jsonl"
        self.calls.touch()
        port_file = self.tmp / "port"
        self.server = subprocess.Popen([sys.executable, str(self.repo / "tests" / "fake_discord.py"), "--port-file",
                                        str(port_file), "--log", str(self.calls), "--guild", GUILD])
        for _ in range(100):
            if port_file.exists() and port_file.read_text():
                break
            time.sleep(0.05)
        self.base = f"http://127.0.0.1:{port_file.read_text()}"
        self.sysstate = self.tmp / "systemctl-state.json"
        self.gateway(active=True, invocation="inv1", restarts=0)
        self.env = {"HOME": str(self.home), "HERMES_HOME": str(self.hh), "PATH": f"{self.bin}:/usr/bin:/bin",
                    "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "FLOW_DISCORD_API": f"{self.base}/api/v10",
                    "FAKE_SYSTEMCTL_LOG": str(self.tmp / "systemctl-calls.jsonl"),
                    "FAKE_SYSTEMCTL_STATE": str(self.sysstate), "FAKE_HERMES_DIR": str(self.tmp / "fake-hermes"),
                    "GIT_AUTHOR_NAME": "test",
                    "GIT_AUTHOR_EMAIL": "test@example.invalid", "GIT_COMMITTER_NAME": "test",
                    "GIT_COMMITTER_EMAIL": "test@example.invalid", "TMPDIR": str(self.tmp)}

    def gateway(self, active, invocation, restarts, scopes=()):
        self.sysstate.write_text(json.dumps({"ActiveState": "active" if active else "failed",
                                             "SubState": "running" if active else "dead", "InvocationID": invocation,
                                             "NRestarts": str(restarts), "ActiveEnterTimestamp": "T",
                                             "scopes": list(scopes)}))

    def close(self):
        self.server.terminate()
        self.server.wait(timeout=10)
        if self.keep:
            print(f"temp-map bewaard: {self.tmp}")
        else:
            shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ helpers
    def sh(self, *cmd, input=None, ok=True, timeout=300):
        r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, env=self.env, input=input,
                           timeout=timeout, cwd=self.repo)
        if ok and r.returncode != 0:
            raise AssertionError(f"{' '.join(map(str, cmd))[:120]} → exit {r.returncode}: {(r.stderr or r.stdout)[-600:]}")
        return r

    def py(self, script, *args, **kw):
        return self.sh(sys.executable, self.hh / "scripts" / script, *args, **kw)

    def state(self):
        with urllib.request.urlopen(f"{self.base}/_test/state") as resp:
            return json.loads(resp.read())

    def api(self, method, path, body=None):
        req = urllib.request.Request(f"{self.base}/api/v10{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None

    def log(self):
        return [json.loads(l) for l in self.calls.read_text().splitlines() if l.strip()]

    def ids(self):
        return json.loads((self.hh / "team" / "discord.json").read_text())

    def messages(self, key):
        return self.state()["messages"].get(self.ids()[key], [])

    def hermes_calls(self):
        # scripts that start hermes with a minimal environment (only HOME/PATH) log under HERMES_HOME
        calls = []
        for f in (self.tmp / "fake-hermes" / "fake-hermes-calls.jsonl", self.hh / "fake-hermes-calls.jsonl"):
            if f.exists():
                calls += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
        return [c["argv"] for c in sorted(calls, key=lambda c: c["at"])]

    def thread_of(self, card):
        return next((c for c in self.state()["channels"].values() if c["type"] == 11 and card in c["name"]), None)

    def tag_names(self, thread):
        forum = self.state()["channels"][thread["parent_id"]]
        names = {t["id"]: t["name"] for t in forum["available_tags"]}
        return sorted(names[t] for t in thread["applied_tags"])

    def step(self, name, fn):
        t0 = time.time()
        try:
            detail = fn() or "ok"
            ok = True
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"{type(e).__name__}: {e}"
            if os.environ.get("FLOW_TEST_DEBUG"):
                traceback.print_exc()
        self.results.append({"stap": name, "ok": ok, "detail": str(detail)[:400], "s": round(time.time() - t0, 1)})
        print(f"{'OK  ' if ok else 'FOUT'} {name}: {str(detail)[:300]}", flush=True)

    # ------------------------------------------------------------------ steps
    def s_install(self):
        r = self.sh("bash", self.repo / "install.sh", "--dry-run")
        assert not (self.hh / "scripts").exists() and not (self.hh / "team").exists(), "dry-run veranderde iets"
        assert "dry-run" in r.stdout
        self.sh("bash", self.repo / "install.sh", "--basis", "--geen-cron")
        cfg = self.hh / "team" / "flow.yaml"
        assert cfg.read_text() == (self.repo / "config.example.yaml").read_text(), "config is geen kopie van het voorbeeld"
        assert oct(cfg.stat().st_mode)[-3:] == "600", "config niet chmod 600"
        assert (self.hh / "scripts" / "team-questions.py").exists()
        unit = (self.home / ".config/systemd/user/hermes-gateway-watch.service").read_text()
        assert "%h/.hermes/scripts/gateway-watch.py" in unit
        sysc = (self.tmp / "systemctl-calls.jsonl").read_text()
        assert "hermes-gateway-watch.timer" in sysc and "daemon-reload" in sysc
        out = self.py("flow_config.py", "check").stdout
        assert "config OK" in out, out
        # the only user input: guild and owner (fake numbers) — like copying them from Discord
        (self.hh / "team" / "discord.json").write_text(json.dumps({"guild": GUILD, "owner": OWNER}))
        # a minimal Hermes config (part of any Hermes install): the dispatcher block the health check reads
        (self.hh / "config.yaml").write_text("kanban:\n  dispatch_in_gateway: true\n  max_in_progress: 3\n"
                                             "  dispatch_profiles: coder,reviewer\n")
        return f"{len(list((self.hh / 'scripts').iterdir()))} bestanden in scripts/, config = voorbeeld, units met %h"

    def s_setup(self):
        dry = self.py("discord_setup.py", "--dry-run").stdout
        assert "aanmaken: vragen" in dry and not [c for c in self.log() if c["method"] != "GET"], "dry-run postte iets"
        self.py("discord_setup.py")
        st = self.state()
        chans = {c["name"]: c for c in st["channels"].values() if c["type"] != 11}
        for n in ("GENERAL", "HERMES AGENTS", "WORKFLOW", "Systeem"):
            assert chans[n]["type"] == 4, n
        for n in ("chatlog", "meldingen", "staging", "samenvatting", "ochtendrapport", "gezondheid", "releases", "regels"):
            assert chans[n]["type"] == 0, n
        forum = chans["vragen"]
        assert forum["type"] == 15 and forum["parent_id"] == chans["HERMES AGENTS"]["id"]
        assert {"open", "beantwoord", "verwerkt"} <= {t["name"] for t in forum["available_tags"]}
        deny = [o for o in chans["meldingen"]["permission_overwrites"] if o["id"] == GUILD][0]
        assert int(deny["deny"]) & (1 << 11), "iedereen mag nog posten in #meldingen"
        assert "COMMUNITY" in st["guild"]["features"]
        ids = self.ids()
        assert ids["vragen"] == forum["id"] and ids["owner"] == OWNER
        n_before = sum(1 for c in self.log() if c["method"] == "POST")
        self.py("discord_setup.py")
        assert sum(1 for c in self.log() if c["method"] == "POST") == n_before, "tweede run maakte iets aan"
        return f"{len(chans)} kanalen/categorieën, forum met labels, rechten, Community; tweede run idempotent"

    def s_cron(self):
        self.sh("bash", self.repo / "install.sh")
        jobs = json.loads((self.tmp / "fake-hermes" / "fake-cron.json").read_text())
        names = {j["name"]: j for j in jobs}
        for n in ("Vraag van het team", "Staging klaar", "Discord-opruimcontrole",
                  "Dagelijkse samenvatting", "Bordbewaking", "Kanban workspace cleanup"):
            assert n in names, f"cronjob {n} ontbreekt"
        assert names["Vraag van het team"]["no_agent"] and names["Vraag van het team"]["schedule"] == "*/2 * * * *"
        assert names["Dagelijkse samenvatting"]["deliver"] == f"discord:{self.ids()['samenvatting']}"
        assert "de eigenaar" in names["Dagelijkse samenvatting"]["prompt"] and not names["Dagelijkse samenvatting"]["no_agent"]
        self.sh("bash", self.repo / "install.sh")
        assert len(json.loads((self.tmp / "fake-hermes" / "fake-cron.json").read_text())) == len(jobs), "dubbele jobs"
        timers = (self.home / ".config/systemd/user/hermes-ochtendrapport.timer").read_text()
        assert "OnCalendar=*-*-* 07:30:00 Europe/Amsterdam" in timers, timers
        return f"{len(jobs)} cronjobs, idempotent; timers met tijden uit de config"

    def board(self):
        self.db = kf.board_db(self.hh, "team")
        self.repo_dir = self.home / "Hermes Workspace" / "projects" / "demo"
        origin = self.tmp / "git" / "demo.git"
        origin.parent.mkdir(parents=True)
        g = lambda *a, cwd=None: self.sh("git", *a) if cwd is None else subprocess.run(  # noqa: E731
            ["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True, env=self.env)
        g("init", "--bare", "-b", "main", origin)
        self.repo_dir.mkdir(parents=True)
        g("init", "-b", "main", cwd=self.repo_dir)
        g("remote", "add", "origin", str(origin), cwd=self.repo_dir)
        (self.repo_dir / "README.md").write_text("demo\n")
        g("add", ".", cwd=self.repo_dir)
        g("commit", "-m", "start", cwd=self.repo_dir)
        g("checkout", "-b", "staging", cwd=self.repo_dir)
        self.git = lambda *a: g(*a, cwd=self.repo_dir)
        self.pid = kf.add_project(self.hh, "demo", "Demo", self.repo_dir, staging_line=(
            f"- Staging: testdeploy (branch `staging`), URL {self.base}/demo"))

    def commit(self, name):
        (self.repo_dir / f"{name}.txt").write_text(name)
        self.git("add", ".")
        self.git("commit", "-m", name)
        self.git("push", "-q", "origin", "staging")
        self.git("fetch", "-q", "origin")
        return self.git("rev-parse", "HEAD").stdout.strip()

    def s_vraag(self):
        self.board()
        prefix = "Vraag voor de eigenaar:"
        kf.add_card(self.db, "t_aaaa0001", "Beslissing: kleur knop", status="todo", project_id=self.pid)
        self.ev1 = kf.block(self.db, "t_aaaa0001", VALID.format(prefix=prefix), kind="needs_input")
        kf.add_card(self.db, "t_aaaa0002", "Beslissing: zonder ster", status="todo", assignee="coder", project_id=self.pid)
        kf.block(self.db, "t_aaaa0002", VALID.format(prefix=prefix).replace(" ⭐ Aanbevolen", ""), kind="needs_input")
        chk = self.py("team-questions.py", "--check", input=VALID.format(prefix=prefix)).stdout
        assert chk.startswith("OK"), chk
        dry = self.py("team-questions.py", "--dry-run").stdout
        assert "TERUG NAAR AFZENDER" in dry and "[knoppen:" in dry
        assert not [c for c in self.log() if "/threads" in c["path"] and c["method"] == "POST"], "dry-run postte"
        self.py("team-questions.py")
        t = self.thread_of("t_aaaa0001")
        assert t and t["name"].startswith("[Demo] t_aaaa0001 Beslissing: kleur knop"), t
        assert self.tag_names(t) == ["Demo", "open"], self.tag_names(t)
        msgs = self.state()["messages"][t["id"]]
        first = msgs[0]
        assert first["content"].startswith(f"<@{OWNER}> ") and first["allowed_mentions"]["users"] == [OWNER]
        buttons = [b for row in first["components"] for b in row["components"]]
        assert buttons[0]["label"].startswith("⭐ 1") and buttons[0]["style"] == 3, buttons[0]
        assert buttons[-1]["label"] == "✏️ Anders…" and buttons[-1]["custom_id"] == f"tq:t_aaaa0001:{self.ev1}:x"
        assert [b["custom_id"] for b in buttons[:2]] == [f"tq:t_aaaa0001:{self.ev1}:1", f"tq:t_aaaa0001:{self.ev1}:2"]
        assert msgs[1]["content"].startswith("**Details**") and msgs[1]["flags"] == 4096, "details niet stil"
        assert self.thread_of("t_aaaa0002") is None, "onvolledige vraag toch gepost"
        back = [c for c in self.hermes_calls() if "t_aaaa0002" in c]
        assert any("comment" in c for c in back) and any("unblock" in c for c in back), back
        self.py("team-questions.py")
        assert len(self.state()["messages"][t["id"]]) == 2, "tweede run postte opnieuw"
        self.thread1, self.msg1 = t, first
        return f"post + 3 knoppen (⭐ eerst, Anders… laatst) + stille details; onvolledige vraag terug ({len(back)} calls)"

    def click(self, thread, msg, label):
        """What the patched gateway does on a click (patch 6/6c): buttons off + "✅ Gekozen", tag beantwoord."""
        self.api("PATCH", f"/channels/{thread['id']}/messages/{msg['id']}",
                 {"content": msg["content"][:1900] + f"\n\n✅ **Gekozen:** {label}", "components": []})
        forum = self.state()["channels"][thread["parent_id"]]
        tags = {t["name"]: t["id"] for t in forum["available_tags"]}
        keep = [x for x in self.state()["channels"][thread["id"]]["applied_tags"]
                if x not in (tags["open"], tags["beantwoord"], tags["verwerkt"])]
        self.api("PATCH", f"/channels/{thread['id']}", {"applied_tags": keep + [tags["beantwoord"]]})

    def s_knop(self):
        label = [b for r in self.msg1["components"] for b in r["components"]][0]["label"]
        self.click(self.thread1, self.msg1, label)
        self.py("team-questions.py")
        posts = json.loads((self.hh / "state" / "questions-posts.json").read_text())
        assert posts["team:t_aaaa0001"]["status"] == "beantwoord", posts
        # the manager processes "Antwoord van de eigenaar (knop): optie 1 — …" (fake hermes = the kanban CLI)
        self.sh(self.bin / "hermes", "kanban", "--board", "team", "comment", "--author", "manager", "t_aaaa0001",
                f"Antwoord van de eigenaar (knop): optie 1 — {label}\n\nUitwerking manager: groen")
        kf.set_status(self.db, "t_aaaa0001", "done", completed=True)
        self.py("team-questions.py")
        self.py("discord-cleanup.py")
        # since 03-10 a script never edits an archived post: beantwoord archives it at once (not locked) and the
        # cleanup leaves it as it is when the card is done (the test still expected the old verwerkt + lock)
        t = self.state()["channels"][self.thread1["id"]]
        assert self.tag_names(t) == ["Demo", "beantwoord"], self.tag_names(t)
        assert t["thread_metadata"]["archived"] and not t["thread_metadata"].get("locked")
        content = self.state()["messages"][t["id"]][0]["content"]
        assert "✅ **Gekozen:** ⭐ 1 · Groen" in content
        return "klik → beantwoord en gearchiveerd; kaart done → post onaangeroerd"

    def s_anders(self):
        kf.add_card(self.db, "t_aaaa0003", "Beslissing: lettertype", status="todo", project_id=self.pid)
        ev = kf.block(self.db, "t_aaaa0003", VALID.format(prefix="Vraag voor de eigenaar:"), kind="needs_input")
        kf.add_card(self.db, "t_aaaa0004", "Beslissing: nog open", status="todo", project_id=self.pid)
        kf.block(self.db, "t_aaaa0004", VALID.format(prefix="Vraag voor de eigenaar:"), kind="needs_input")
        self.py("team-questions.py")
        t = self.thread_of("t_aaaa0003")
        msg = self.state()["messages"][t["id"]][0]
        other = [b for r in msg["components"] for b in r["components"]][-1]
        assert other["custom_id"] == f"tq:t_aaaa0003:{ev}:x"
        # patch 6b/6c: the modal (custom_id tqm:<task>:<episode>) is submitted → buttons off, tag beantwoord
        self.click(t, msg, "✏️ Anders… → serif, maar groter")
        # the manager writes the wait state (patch 7: a "Wacht op …" reason replaces the question)
        kf.block(self.db, "t_aaaa0003", "Wacht op de bouwer – antwoord 'serif, maar groter' gegeven", kind="needs_input")
        n_threads = sum(1 for c in self.state()["channels"].values() if c["type"] == 11)
        self.py("team-questions.py")
        assert sum(1 for c in self.state()["channels"].values() if c["type"] == 11) == n_threads, "wachtstand gepost"
        posts = json.loads((self.hh / "state" / "questions-posts.json").read_text())
        assert posts["team:t_aaaa0003"]["status"] == "beantwoord", posts
        status = self.py("team-status.py").stdout
        assert "t_aaaa0003 blocked" in status and "Wacht op de bouwer" in status, status
        assert "VRAAG AAN DE EIGENAAR: OPEN" in status, status  # t_aaaa0004 is still open
        return "Anders… → beantwoord; wachtstand niet gepost; team-status toont de wachtstand en de open vraag"

    def s_melding(self):
        self.py("discord_post.py", "meldingen", "Testmelding")
        m = self.messages("meldingen")[-1]
        assert m["content"] == f"<@{OWNER}> Testmelding" and m["allowed_mentions"]["users"] == [OWNER]
        kf.add_card(self.db, "t_aaaa0005", "Bouwkaart met keuze", status="todo", assignee="coder", project_id=self.pid)
        kf.block(self.db, "t_aaaa0005", "beslissing manager: welke tabel?", ago=45 * 60)
        n = len(self.messages("meldingen"))
        self.py("board-guard.py")
        assert len(self.messages("meldingen")) == n, "een blokkade hoort niet in #meldingen"
        assert any(c[:1] == ["chat"] and "t_aaaa0005" in " ".join(c) for c in self.hermes_calls()), "manager niet gewekt"
        kf.add_card(self.db, "t_aaaa0006", "Bouwkaart mislukt", status="blocked", assignee="coder", project_id=self.pid)
        with kf.conn(self.db) as c:
            c.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES ('t_aaaa0006', 'crashed', "
                      "NULL, ?)", (int(time.time()) - 60,))
        self.py("board-guard.py")
        m = self.messages("meldingen")[-1]
        assert "Kaart mislukt: t_aaaa0006" in m["content"] and m["content"].startswith(f"<@{OWNER}>"), m["content"]
        n = len(self.messages("meldingen"))
        self.py("board-guard.py")
        assert len(self.messages("meldingen")) == n, "dubbele melding"
        kf.set_status(self.db, "t_aaaa0006", "done", completed=True)
        self.py("discord-cleanup.py")
        edited = [x for x in self.messages("meldingen") if "t_aaaa0006" in x["content"]][0]
        assert edited["content"].startswith("✅ opgelost (") and "~~" in edited["content"], edited["content"]
        return "ping-melding; blokkade → manager gewekt, geen melding; kaart mislukt één keer, daarna ✅ opgelost"

    def s_staging(self):
        c1 = self.commit("f1-01")
        kf.add_card(self.db, "t_bbbb0001", "F1-01 Inloggen", status="done", project_id=self.pid, completed_ago=900)
        kf.add_run(self.db, "t_bbbb0001", metadata={"merged_commit": c1})
        self.py("staging-announce.py")  # first run: one summary, phases recorded as baseline
        m = self.messages("staging")
        assert len(m) == 1 and m[0]["content"].startswith("✅ Staging klaar (samenvatting, 1 kaarten)") and m[0]["flags"] == 4096
        c2 = self.commit("f1-02")
        kf.add_card(self.db, "t_bbbb0002", "F1-02 Uitloggen", status="done", project_id=self.pid, completed_ago=300)
        kf.add_run(self.db, "t_bbbb0002", metadata={"merged_commit": c2})
        kf.add_card(self.db, "t_bbbb0099", "F1-99 Eindcontrole fase 1", status="done", project_id=self.pid,
                    completed_ago=120)
        self.py("staging-announce.py")
        m = self.messages("staging")
        assert m[-1]["content"] == f"[Demo] ✅ t_bbbb0002 F1-02 Uitloggen — {self.base}/demo" and m[-1]["flags"] == 4096
        done = [x for x in self.messages("meldingen") if "Fase F1 compleet" in x["content"]]
        assert done and done[0]["content"].startswith(f"<@{OWNER}> 🏁 [Demo] Fase F1 compleet op staging"), done
        created = [c for c in self.hermes_calls() if c[:4] == ["kanban", "--board", "team", "create"]]
        assert created and created[-1][4] == "Beslissing: fase F1 naar productie?", created
        self.py("team-questions.py")  # the production question reaches #vragen like any other
        t = self.thread_of("fase F1 naar productie")
        assert t is not None, "productievraag niet in #vragen"
        self.py("staging-announce.py")
        assert len(self.messages("staging")) == len(m), "dubbele staging-regel"
        return "samenvatting bij de eerste run, daarna één stille regel per kaart; fase compleet + productievraag"

    def s_samenvatting(self):
        out = self.py("daily-summary-data.py").stdout
        for part in ("Actieve projecten: demo", "AF (gisteren)", "BEZIG", "VAST", "METING"):
            assert part in out, (part, out[:400])
        return "gegevens voor de samenvatting; cronjob levert aan #samenvatting (zie cron)"

    def s_ochtendrapport(self):
        dry = self.py("hermes-ochtendrapport.py", "--dry-run").stdout
        assert dry.startswith("**Ochtendrapport workflow") and "(modus: opstart)" in dry
        n = len(self.messages("ochtendrapport"))
        self.py("hermes-ochtendrapport.py")
        m = self.messages("ochtendrapport")[n:]  # het rapport wordt op secties gesplitst; alleen het eerste pingt
        assert m and m[0]["content"].startswith(f"<@{OWNER}> **Ochtendrapport workflow")
        assert all(f"<@{OWNER}>" not in x["content"] for x in m[1:]), "meer dan één ping"
        alles = "\n".join(x["content"] for x in m)
        for kop in ("**Gezondheid**", "**Nacht**", "**Uitgevoerd / zelf opgelost**", "**Open workflowpunten**"):
            assert kop in alles, kop
        return f"rapport in {len(m)} bericht(en) met één ping in #ochtendrapport"

    def s_inbox(self):
        inbox = self.hh / "workflow-inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        old = time.time() - 3600
        item = inbox / "20261001-0900-voorbeeldpunt.md"
        item.write_text("# Nieuwe timer voor de samenvatting\nWat: De samenvatting moet een uur later. Dat vraagt een "
                        "nieuwe timer. Derde zin.\nWie vroeg het: manager\nSinds: 2026-10-01 09:00\n", encoding="utf-8")
        secret = inbox / "20261001-0901-geheim.md"
        secret.write_text("# Verbinding\nWat: postgres://gebruiker:ietsgeheims123@example.invalid/x\n"
                          "Wie vroeg het: manager\n", encoding="utf-8")
        for f in (item, secret):
            os.utime(f, (old, old))
        self.py("workflow-inbox.py", "sync")
        m = self.messages("workflow-inbox")
        assert len(m) == 2 and "Status: open" in m[0]["content"] and "Project: workflow" in m[0]["content"], m
        assert "ietsgeheims123" not in m[1]["content"] and "niet getoond" in m[1]["content"], m[1]["content"]
        self.py("workflow-inbox.py", "sync")
        assert len(self.messages("workflow-inbox")) == 2, "tweede run plaatste opnieuw"
        line = self.py("workflow-inbox.py", "status").stdout.strip()
        assert line == "Workflow-inbox: 2 open (oudste: 01-10 09:00)", line
        self.py("workflow-inbox.py", "afhandelen", item.name, "Timer aangepast (release 1).")
        m = self.messages("workflow-inbox")
        assert len(m) == 2 and m[0]["content"].startswith("~~**📥 Nieuwe timer") and "✅ Afgehandeld" in m[0]["content"]
        assert m[0].get("edited") and m[0].get("reactions"), m[0]
        assert (inbox / "afgehandeld" / item.name).exists() and not item.exists()
        secret.unlink()
        assert "Workflow-inbox: niets open" in self.py("hermes-ochtendrapport.py", "--dry-run").stdout
        return "punt geplaatst (Status: open), geen dubbele, geheim verborgen; afgehandeld = zelfde bericht doorgestreept + ✅; ochtendrapportregel"

    def s_vraagfilter(self):
        kf.add_card(self.db, "t_00000f01", "Preflight-blokkade", status="blocked", assignee="coder", project_id=self.pid)
        kf.block(self.db, "t_00000f01", "worker preflight: worktree branch does not contain repository HEAD",
                 kind="needs_input", ago=600)
        dry = self.py("team-questions.py", "--dry-run").stdout
        assert "t_00000f01" not in dry, dry[-600:]
        guard = self.py("board-guard.py", "--dry-run").stdout
        assert "t_00000f01" in guard and "blokkade-voor-manager" in guard, guard[-600:]
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE tasks SET status = 'archived' WHERE id = 't_00000f01'")
        conn.commit()
        conn.close()
        return "needs_input zonder vraag: vraagcontrole laat hem liggen, bordbewaking meldt blokkade-voor-manager"

    def s_gezondheid(self):
        leftover = "hermes-worker-kanban-t_cccc0001-run-77.scope loaded active running worker"
        self.gateway(active=False, invocation="inv1", restarts=0, scopes=[leftover])
        r = self.py("hermes-health.py", "--dry-run").stdout
        assert "FOUT! gateway" in r and "FOUT worker-resten" in r, r
        self.py("hermes-health.py")
        h = json.loads((self.hh / "state" / "health.json").read_text())
        assert not h["checks"]["gateway"]["ok"] and h["checks"]["gateway"]["blocking"]
        red = [x for x in self.messages("meldingen") if "Gateway faalt sinds" in x["content"]]
        assert len(red) == 1 and red[0]["content"].startswith(f"<@{OWNER}> 🔴"), red
        quiet = self.messages("gezondheid")
        assert all(x["flags"] == 4096 and "<@" not in x["content"] for x in quiet), quiet
        assert any("Worker-resten" in x["content"] for x in quiet), [x["content"] for x in quiet]
        bad = [k for k, c in h["checks"].items() if not c["ok"] and not c["blocking"]]
        assert len(quiet) == len(bad), (bad, [x["content"] for x in quiet])
        self.py("hermes-health.py")
        assert len([x for x in self.messages("meldingen") if "Gateway faalt" in x["content"]]) == 1, "dubbel"
        self.gateway(active=True, invocation="inv1", restarts=0)
        self.py("hermes-health.py")
        fixed = [x for x in self.messages("meldingen") if "Gateway faalt" in x["content"]][0]
        assert fixed["content"].startswith("✅ opgelost ("), fixed["content"]
        worker = [x for x in self.messages("gezondheid") if "Worker-resten" in x["content"]][0]
        assert worker["content"].startswith("✅ opgelost ("), worker["content"]
        return f"blokkerend → #meldingen (ping), {len(quiet)} stille melding(en) in #gezondheid, geen dubbele, ✅ opgelost"

    def s_release(self):
        r = self.py("hermes-release.py", "--notitie", "eerste installatie", ok=False, timeout=600)
        assert r.returncode == 0, r.stdout + r.stderr
        m = self.messages("releases")
        assert len(m) == 1 and m[0]["flags"] == 4096 and " — eerste installatie (regressie " in m[0]["content"], m
        reg = json.loads((self.hh / "state" / "regressie.json").read_text())
        assert reg["ok"] and reg["mode"] == "quick"
        return m[0]["content"]

    def s_bordbewaking(self):
        empty = self.tmp / "leeg"
        kf.board_db(empty / ".hermes", "team")
        (empty / ".hermes" / "team").mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.hh / "team" / "discord.json", empty / ".hermes" / "team" / "discord.json")
        env = {**self.env, "HERMES_HOME": str(empty / ".hermes"), "HERMES_FLOW_CONFIG": str(self.hh / "team" / "flow.yaml")}
        n_calls = len(self.log())
        r = subprocess.run([sys.executable, self.hh / "scripts" / "board-guard.py", "--dry-run"], capture_output=True,
                           text=True, env=env, timeout=120)
        assert r.returncode == 0 and r.stdout.strip().endswith("0 afwijking(en)"), r.stdout + r.stderr
        r = subprocess.run([sys.executable, self.hh / "scripts" / "board-guard.py"], capture_output=True, text=True,
                           env=env, timeout=120)
        assert r.returncode == 0, r.stderr
        assert len(self.log()) == n_calls, "bordbewaking praatte met Discord op een leeg bord"
        found = self.py("board-guard.py", "--dry-run").stdout  # the demo board: the open question has a post
        return f"leeg bord: 0 afwijkingen, niets gepost; demo-bord: {found.strip().splitlines()[-1]}"

    def s_opruiming(self):
        out = self.py("werkmap_groei.py", "--dry-run").stdout
        assert out.startswith("== clean_kanban_workspaces --dry-run:"), out
        rep = json.loads(self.py("werkmap_groei.py", "--groei").stdout)
        assert rep["ok"] and "totaal" in rep
        return out.splitlines()[0]

    def s_gateway(self):
        self.py("gateway-watch.py")  # baseline
        self.gateway(active=True, invocation="inv2", restarts=1)
        self.py("gateway-watch.py")
        m = [x for x in self.messages("meldingen") if "Gateway onverwacht herstart" in x["content"]]
        assert len(m) == 1 and "crashte" in m[0]["content"], m
        return "baseline stil; onverwachte herstart → #meldingen"

    def s_privacy(self):
        allow = self.tmp / "allowlist.txt"  # the test project of this run is local data, not personal
        meegeleverd = self.repo / "scan-allowlist.txt"  # de beoordeelde uitzonderingen van het pakket gelden ook hier
        allow.write_text("*\t(?i)^demo$\t# testproject van deze test\n"
                         + (meegeleverd.read_text() if meegeleverd.exists() else ""))
        scan = self.repo / "scripts" / "scan_personal.py"
        r = self.sh(sys.executable, scan, "--allowlist", allow, self.repo, ok=False)
        assert r.returncode == 0, r.stdout[-800:]
        # Alleen wat er naar Discord geschreven wordt; GET-verzoeken bevatten berekende tijdgrenzen (before=<snowflake>).
        posted = "\n".join(json.dumps(c, ensure_ascii=False) for c in self.log() if str(c[0] if isinstance(c, list) else c.get("method", "")).upper() != "GET")
        dump = self.tmp / "gepost.txt"
        dump.write_text(posted)
        r2 = self.sh(sys.executable, scan, "--allowlist", allow, dump, ok=False)
        assert r2.returncode == 0, r2.stdout[-800:]
        assert self.real_home not in posted, "echte thuismap in een Discord-call"
        for f in list(self.hh.rglob("*.json")) + list(self.hh.rglob("*.log")) + list(self.hh.rglob("*.jsonl")):
            assert self.real_home + "/" not in f.read_text(errors="replace"), f"echte thuismap in {f.name}"
        return f"scanner schoon (repo + {len(self.log())} Discord-calls); geen echte thuismap in state of logs"

    def s_patches(self, checkout, python, pythonpath):
        tests = [l.split()[0] for l in (self.repo / "patches" / "TESTS.txt").read_text().splitlines()
                 if l.strip() and not l.startswith("#") and "discord" in l.split()[0] and l.split()[0].endswith(".py")]
        assert tests, "geen knop-tests in patches/TESTS.txt"
        env = {"HOME": os.environ.get("HOME", ""), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TMPDIR": str(self.tmp),
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ":".join([checkout] + ([pythonpath] if pythonpath else []))}
        r = subprocess.run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", *tests],
                           capture_output=True, text=True, env=env, cwd=checkout, timeout=900)
        last = (r.stdout.strip().splitlines() or ["?"])[-1]
        assert r.returncode == 0, r.stdout[-1500:]
        return f"{', '.join(tests)}: {last}"


def main(argv):
    keep = "--bewaar" in argv
    opt = lambda k: argv[argv.index(k) + 1] if k in argv else None  # noqa: E731
    run = Run(keep)
    try:
        run.prepare()
        for name in ("install", "setup", "cron", "vraag", "knop", "anders", "melding", "staging", "samenvatting",
                     "ochtendrapport", "inbox", "gezondheid", "release", "bordbewaking", "vraagfilter", "opruiming",
                     "gateway", "privacy"):
            run.step(name, getattr(run, f"s_{name}"))
        if opt("--hermes"):
            run.step("patches", lambda: run.s_patches(opt("--hermes"), opt("--pytest-python") or sys.executable,
                                                      opt("--pythonpath")))
    finally:
        report = run.tmp / "rapport.json"
        if run.tmp.exists():
            report.write_text(json.dumps(run.results, ensure_ascii=False, indent=1))
        run.close()
    ok = sum(r["ok"] for r in run.results)
    print(f"\nSchone installatie: {ok}/{len(run.results)} stappen OK")
    return 0 if ok == len(run.results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
