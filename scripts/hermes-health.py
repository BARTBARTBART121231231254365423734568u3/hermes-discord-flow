#!/usr/bin/env python3
"""Health check of the Hermes team setup (systemd --user timer hermes-health.timer, every 30 min at :07/:37).
No model, no API calls that cost tokens; every probe is read-only and has its own timeout (total < 60 s).

Personal values (projects, paths, thresholds, texts) come from the flow config (flow_config.py).

Checks (key in health.json → what fails it):
  model-login            the login wrapper (gezondheid.model_login.wrapper, relative to HERMES_HOME; empty = not
                         checked) missing/not executable (or the binary it execs), the command variable
                         (gezondheid.model_login.env_variabele; empty = not checked) in ~/.hermes/.env or
                         profiles/*/.env pointing to a missing/non-executable file (only that variable is read), or
                         "Primary (provider) auth failed" in ~/.hermes/logs/agent.log in the last 30 min. BLOCKING
                         when no login works at all (wrapper or every configured command broken).
  db-login:<slug>        psql 'select 1' with the default URL from the project's source (projecten[].db.bestand);
                         BLOCKING for an ACTIEF project.
  pg-log                 'password authentication failed' / 'ALTER ROLE' in the PostgreSQL log (gezondheid.postgres_log;
                         empty = skipped), last 30 min (only counts and times are reported, never the lines themselves).
  git:<slug>             git ls-remote --heads origin (20 s).
  staging:<slug>         HTTP 200 on <staging URL><health path>; the URL comes from the "Staging: … URL https://…"
                         line of ~/.hermes/team/projects/<slug>.md; redirects count as a failure.
  gateway                systemctl --user is-active hermes-gateway.service (BLOCKING).
  dispatcher             kanban.dispatch_in_gateway false, or kanban.dispatch_profiles empty without a drain
                         (~/.hermes/state/drain.json) or with a drain older than 2 h (BLOCKING).
  dispatcher-stil        no "kanban dispatcher" line in agent.log for 10 min while a card is ready for > 5 min
                         whose assignee has nothing running and there is room (the dispatcher only logs when
                         it spawns, so silence alone is normal).
  worker-resten          hermes-worker-kanban-<task>-run-<id>.scope still loaded while that run ended > 30 min ago.
  schijf / cpu / swap / geheugen   free on / < 10 GB; /proc/pressure/cpu some avg300 > 60; swap used > 2.5 GB;
                         MemAvailable < 400 MB.
  ops-checkin            (only when gezondheid.ops_checkin) ~/.hermes/state/ops-checkin.json {"at": epoch} older
                         than drempels.checkin_max_uur or missing (the ops session writes it hourly).
  werkmappen             (only when WERKMAPPEN_AAN = gezondheid.werkmappen) card work folders (scratch workspaces + git
                         worktrees, see werkmap_groei.py): more leftovers than drempels.werkmappen_resten_max or more
                         than drempels.werkmappen_totaal_max_gb in total; the detail names the counts, sizes and the
                         largest folders.

Routing per failure episode (event register key "health:<check>:<since>", see discord_post.py):
- BLOCKING → one message with ping in #meldingen;
- everything else → one silent message in #gezondheid (channel key "gezondheid" in team/discord.json; when it
  does not exist yet only the log and the state file are written);
- a check that is OK again: its message is edited to "✅ opgelost (HH:MM) — ~~…~~" and marked resolved.
Always writes ~/.hermes/state/health.json (per check: ok, detail, since, last_run) for the morning report, and
~/.hermes/state/zelfcontrole-nodig.json {"at", "reasons"} when a check fails or ~/.hermes/workflow-inbox/ has a
*.md (not LEESMIJ.md) newer than the last ops check-in; that flag file is removed when nothing needs attention.
``--dry-run`` runs the probes, prints results and planned actions, and writes, posts and edits nothing.
Log: ~/.hermes/logs/hermes-health.log.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discord_post as dp  # noqa: E402
import flow_config as fc  # noqa: E402
import werkmap_groei  # noqa: E402
from team_projects import HOME, active_projects, boards, kanban  # noqa: E402

# Projects to check (slug = file name in ~/.hermes/team/projects/), from the flow config "projecten":
# "db": where the default DATABASE_URL lives (None = no DB check); "health": health path on the staging URL;
# "staging" may override the URL from the project file.
PROJECTS = {slug: {"repo": p["repo"], "health": p.get("health_pad") or "/health", "db": p.get("db") or None,
                   **({"staging": str(p["staging_url"]).rstrip("/")} if fc.is_set(p.get("staging_url")) else {})}
            for slug, p in fc.projects().items()}

LOG = HOME / "logs" / "hermes-health.log"
AGENT_LOG = HOME / "logs" / "agent.log"
PG_LOG = Path(fc.get("gezondheid.postgres_log")) if fc.is_set(fc.get("gezondheid.postgres_log")) else None
STATE = HOME / "state" / "health.json"
FLAG = HOME / "state" / "zelfcontrole-nodig.json"
CHECKIN = HOME / "state" / "ops-checkin.json"
DRAIN = HOME / "state" / "drain.json"
INBOX = fc.path("workflow_inbox")
_LOGIN = fc.get("gezondheid.model_login.wrapper")
LOGIN = fc.expand(_LOGIN) if fc.is_set(_LOGIN) else None
LOGIN_ENV = fc.get("gezondheid.model_login.env_variabele") or ""
UNIT = fc.get("gateway.unit")
NL = fc.tz()
D = fc.get("drempels")

WINDOW = 30 * 60
DISPATCH_QUIET = 10 * 60
READY_GRACE = 5 * 60
DRAIN_MAX = 2 * 3600
DRAIN_GRACE = 15 * 60  # drain-restart.py waits max 20 min (until), then restarts or restores; 15 min margin
LEFTOVER_AFTER = 30 * 60
CHECKIN_MAX = D["checkin_max_uur"] * 3600
DISK_MIN = D["schijf_min_gb"] * 1024 ** 3
CPU_MAX = float(D["cpu_max_procent"])
SWAP_MAX = D["swap_max_gb"] * 1024 ** 3
MEM_MIN = D["geheugen_min_mb"] * 1024 ** 2
WERKMAPPEN_AAN = bool(fc.get("gezondheid.werkmappen"))
OPS_CHECKIN_AAN = bool(fc.get("gezondheid.ops_checkin"))

SECRETS_DIR = Path(os.environ.get("HERMES_SECRETS_DIR") or HOME / "secrets")
URL_RE = re.compile(r"postgres(?:ql)?://([^:/@\s\"']+):([^@\s\"']+)@([^/\s:\"']+)(?::(\d+))?/([A-Za-z0-9_\-]+)")
STAMP_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
SCOPE_RE = re.compile(r"^(hermes-worker-kanban-(t_[0-9a-f]+)-run-(\d+)\.scope)\s")
AUTH_FAIL = ("Primary provider auth failed", "Primary auth failed")
_SECRETS = set()  # values that must never reach output (filled while probing)


def redact(text: str) -> str:
    text = re.sub(r"://[^@/\s]+@", "://***@", text or "")
    for s in _SECRETS:
        text = text.replace(s, "***")
    return text


def hhmm(ts) -> str:
    return datetime.fromtimestamp(ts, NL).strftime("%H:%M") if ts else "?"


def result(check, ok, detail, blocking=False, label=None):
    return {"check": check, "ok": bool(ok), "detail": redact(detail)[:300], "blocking": bool(blocking and not ok),
            "label": label or check}


def run(cmd, timeout, env=None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def lines_since(path: Path, since: float, needles, max_bytes=4 * 1024 ** 2):
    """[(ts, line)] of lines with a leading UTC timestamp >= since containing one of ``needles``; reads the tail
    of ``path`` and, when that tail starts inside the window, also the rotated ``path.1``."""
    found = []
    for p in (path, path.with_name(path.name + ".1")):
        try:
            size = p.stat().st_size
            with p.open("rb") as fh:
                fh.seek(max(0, size - max_bytes))
                chunk = fh.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            break
        first = None
        for line in chunk:
            m = STAMP_RE.match(line)
            if not m:
                continue
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
            first = ts if first is None else first
            if ts >= since and any(n in line for n in needles):
                found.append((ts, line))
        if size > max_bytes or (first is not None and first < since):
            break  # the window starts in this file
    return sorted(found)


def project_list() -> dict:
    """{slug: {..PROJECTS entry, "status", "staging"}} for PROJECTS whose project file is ACTIEF or GEPAUZEERD."""
    live = active_projects(("ACTIEF", "GEPAUZEERD"))
    out = {}
    for slug, cfg in PROJECTS.items():
        if slug not in live:
            continue
        text = live[slug]["file"].read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^\s*-\s*Staging:.*?\b(https://[^\s)`]+)", text, re.M)
        out[slug] = {**cfg, "status": live[slug]["status"], "name": live[slug]["name"],
                     "staging": cfg.get("staging") or (m.group(1).rstrip("/.") if m else None)}
    return out


# ------------------------------------------------------------------ probes (each returns a list of results)
def _executable(path: str) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def probe_model_login(now=None):
    now = now or time.time()
    problems, broken, total = [], 0, 0
    if LOGIN is None:
        wrapper_ok = True  # no login wrapper configured
    elif not _executable(str(LOGIN)):
        problems.append(f"{LOGIN.name} ontbreekt of is niet uitvoerbaar")
        wrapper_ok = False
    else:
        m = re.search(r"^\s*exec\s+(\S+)", LOGIN.read_text(errors="replace"), re.M)
        wrapper_ok = not m or _executable(m.group(1))
        if not wrapper_ok:
            problems.append(f"{LOGIN.name} start {m.group(1)}, die ontbreekt")
    for env in ([HOME / ".env"] + sorted(HOME.glob("profiles/*/.env"))) if LOGIN_ENV else []:
        try:
            text = env.read_text(errors="replace")
        except OSError:
            continue
        m = re.search(rf"^{re.escape(LOGIN_ENV)}=(.*)$", text, re.M)
        if not m:
            continue
        total += 1
        try:
            cmd = shlex.split(m.group(1).strip())[0]
        except (ValueError, IndexError):
            cmd = ""
        if not _executable(os.path.expanduser(cmd)):
            broken += 1
            problems.append(f"{env.relative_to(HOME)} wijst naar een ontbrekend bestand")
    hits = lines_since(AGENT_LOG, now - WINDOW, AUTH_FAIL)
    if hits:
        problems.append(f"{len(hits)}× 'Primary auth failed' in agent.log (laatste {hhmm(hits[-1][0])})")
    all_down = not wrapper_ok or (total and broken == total)
    wrapper = "login-wrapper ok" if LOGIN is not None else "geen login-wrapper ingesteld"
    detail = "; ".join(problems) or f"{wrapper}, {total} .env-verwijzing(en) ok, geen auth-fouten in 30 min"
    return [result("model-login", not problems, detail, blocking=all_down, label="Model-login")]


def db_url(repo: Path, rel: str):
    m = URL_RE.search((repo / rel).read_text(encoding="utf-8", errors="replace"))
    if not m:
        return None
    user, password, _host, port, db = m.groups()
    _SECRETS.add(password)
    return user, password, port or "5432", db


def probe_db():
    out = []
    for slug, p in project_list().items():
        if not p.get("db"):
            continue
        label = f"DB-login {p['name']}"
        try:
            secret = SECRETS_DIR / f"{slug}-db.url"  # 600-file of the ops side (01-10: no default URL in the code)
            url = db_url(SECRETS_DIR, secret.name) if secret.exists() else db_url(p["repo"], p["db"])
            env_only = not url and "DATABASE_URL" in (p["repo"] / p["db"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            url, env_only = None, False
        if env_only:  # the code no longer has a default URL: tests use a throwaway database (pg_virtualenv)
            out.append(result(f"db-login:{slug}", True, f"{p['db']} heeft geen standaard-URL meer (alleen DATABASE_URL); "
                              "rolwijzigingen bewaakt via pg-log", label=label))
            continue
        if not url:
            out.append(result(f"db-login:{slug}", False, f"geen postgres-URL gevonden in {p['db']}",
                              blocking=p["status"] == "ACTIEF", label=label))
            continue
        user, password, port, db = url
        env = {**os.environ, "PGPASSWORD": password, "PGCONNECT_TIMEOUT": "8"}
        try:
            r = run(["psql", "-w", "-h", "localhost", "-p", port, "-U", user, "-d", db, "-tAc", "select 1"], 10, env)
            ok = r.returncode == 0 and r.stdout.strip() == "1"
            why = (r.stderr.strip().splitlines() or ["?"])[-1]
        except (OSError, subprocess.TimeoutExpired) as exc:
            ok, why = False, type(exc).__name__
        detail = f"{user}@localhost/{db} ok" if ok else f"{user}@localhost/{db}: {why}"
        out.append(result(f"db-login:{slug}", ok, detail, blocking=p["status"] == "ACTIEF", label=label))
    return out


def probe_pg_log(now=None):
    now = now or time.time()
    if PG_LOG is None:
        return [result("pg-log", True, "geen PostgreSQL-log ingesteld; overgeslagen", label="PostgreSQL-log")]
    if not os.access(PG_LOG, os.R_OK):
        return [result("pg-log", True, f"{PG_LOG} niet leesbaar voor {os.environ.get('USER', 'deze gebruiker')}; "
                       "overgeslagen", label="PostgreSQL-log")]
    auth = lines_since(PG_LOG, now - WINDOW, ("password authentication failed",))
    alter = lines_since(PG_LOG, now - WINDOW, ("ALTER ROLE",))
    parts = []
    if auth:
        users = sorted(set(re.findall(r'for user "([^"]+)"', " ".join(line for _t, line in auth))))
        parts.append(f"{len(auth)}× wachtwoord geweigerd ({', '.join(users) or '?'}; laatste {hhmm(auth[-1][0])})")
    if alter:
        parts.append(f"{len(alter)}× ALTER ROLE (laatste {hhmm(alter[-1][0])})")
    return [result("pg-log", not parts, "; ".join(parts) or "geen login-fouten of rolwijzigingen in 30 min",
                   label="PostgreSQL-log")]


def probe_git():
    out = []
    for slug, p in project_list().items():
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false"}
        try:
            r = run(["git", "-C", str(p["repo"]), "ls-remote", "--heads", "origin"], 20, env)
            ok = r.returncode == 0 and bool(r.stdout.strip())
            why = (r.stderr.strip().splitlines() or ["geen heads"])[-1]
        except (OSError, subprocess.TimeoutExpired) as exc:
            ok, why = False, type(exc).__name__
        n = len(r.stdout.splitlines()) if ok else 0
        out.append(result(f"git:{slug}", ok, f"origin bereikbaar ({n} branches)" if ok else f"origin: {why}",
                          label=f"Git {p['name']}"))
    return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http_status(url, timeout=15):
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-health/1"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def probe_staging():
    out = []
    for slug, p in project_list().items():
        label = f"Staging {p['name']}"
        if not p.get("staging"):
            out.append(result(f"staging:{slug}", False, "geen staging-URL in het projectbestand", label=label))
            continue
        url = p["staging"] + p["health"]
        try:
            code = http_status(url)
            why = f"HTTP {code}"
        except (OSError, urllib.error.URLError) as exc:
            code, why = None, str(getattr(exc, "reason", exc))[:80] or type(exc).__name__
        out.append(result(f"staging:{slug}", code == 200, f"{url} → {why}", label=label))
    return out


def probe_gateway():
    try:
        state = run(["systemctl", "--user", "is-active", UNIT], 15).stdout.strip() or "?"
    except (OSError, subprocess.TimeoutExpired) as exc:
        state = type(exc).__name__
    return [result("gateway", state == "active", f"{UNIT}: {state}", blocking=True, label="Gateway")]


def kanban_config() -> dict:
    """Keys of the ``kanban:`` block in config.yaml (flat, as strings)."""
    conf, inside = {}, False
    for line in (HOME / "config.yaml").read_text(encoding="utf-8", errors="replace").splitlines():
        if re.match(r"^kanban:\s*$", line):
            inside = True
        elif inside and re.match(r"^\S", line):
            break
        elif inside:
            m = re.match(r"^\s+([A-Za-z_]+):\s*(.*?)\s*$", line)
            if m:
                conf[m.group(1)] = m.group(2).split(" #")[0].strip().strip("'\"")
    return conf


def probe_dispatcher(now=None):
    now = now or time.time()
    conf = kanban_config()
    profiles = [p.strip() for p in conf.get("dispatch_profiles", "").split(",") if p.strip()]
    ok, detail = True, f"dispatch_profiles: {', '.join(profiles)}"
    if conf.get("dispatch_in_gateway", "true").lower() in ("false", "no", "off", "0"):
        ok, detail = False, "kanban.dispatch_in_gateway staat uit"
    elif not profiles:
        try:
            info = json.loads(DRAIN.read_text())
            drain_at = float(info.get("at", 0))
            until = float(info.get("until") or drain_at + DRAIN_MAX)
        except (OSError, ValueError):
            drain_at = until = None
        if drain_at is None:
            ok, detail = False, "dispatch_profiles is leeg en er loopt geen drain"
        elif now > until + DRAIN_GRACE or now - drain_at > DRAIN_MAX:
            ok, detail = False, (f"drain actief sinds {hhmm(drain_at)} en had uiterlijk {hhmm(until)} klaar moeten zijn; "
                                 "dispatcher staat nog uit")
        else:
            detail = f"drain actief sinds {hhmm(drain_at)}, tot uiterlijk {hhmm(until)} (geplande herstart, geen storing)"
    out = [result("dispatcher", ok, detail, blocking=True, label="Dispatcher")]
    stall = ""
    if ok and profiles and not lines_since(AGENT_LOG, now - DISPATCH_QUIET, ("kanban dispatcher",)):
        waiting, running, busy = [], 0, set()
        for _board, db in boards():
            conn = kanban(db)
            for r in conn.execute("SELECT assignee FROM tasks WHERE status = 'running'"):
                running += 1
                busy.add(r["assignee"])
            for t in conn.execute("SELECT t.id, t.assignee, (SELECT MAX(created_at) FROM task_events e "
                                  "WHERE e.task_id = t.id) AS last FROM tasks t WHERE t.status = 'ready'"):
                if t["assignee"] in profiles and t["assignee"] not in busy \
                        and now - (t["last"] or now) > READY_GRACE:
                    waiting.append(t["id"])
        if waiting and running < int(conf.get("max_in_progress") or 3):
            stall = f"10 min geen dispatcher-activiteit terwijl {', '.join(waiting[:5])} klaarstaat"
    out.append(result("dispatcher-stil", not stall, stall or "geen wachtende kaarten die blijven liggen",
                      label="Dispatcher-activiteit"))
    return out


def probe_workers(now=None):
    now = now or time.time()
    listing = run(["systemctl", "--user", "list-units", "--type=scope", "--plain", "--no-legend"], 15).stdout
    runs = {}
    for _board, db in boards():
        conn = kanban(db)
        for line in listing.splitlines():
            m = SCOPE_RE.match(line.strip())
            if m and m.group(3) not in runs:
                row = conn.execute("SELECT status, started_at, ended_at FROM task_runs WHERE id = ? AND task_id = ?",
                                   (int(m.group(3)), m.group(2))).fetchone()
                if row:
                    runs[m.group(3)] = (m.group(1), row)
    left = []
    for line in listing.splitlines():
        m = SCOPE_RE.match(line.strip())
        if not m:
            continue
        if m.group(3) not in runs:
            left.append(f"{m.group(2)} run {m.group(3)} (run onbekend)")
            continue
        _scope, row = runs[m.group(3)]
        ended = row["ended_at"] or row["started_at"] or now
        if row["status"] != "running" and now - ended > LEFTOVER_AFTER:
            left.append(f"{m.group(2)} run {m.group(3)} ({row['status']} sinds {hhmm(ended)})")
    return [result("worker-resten", not left, "; ".join(left) if left else "geen achtergebleven worker-scopes",
                   label="Worker-resten")]


def probe_herstartlus(now=None):
    """User services stuck restarting (incident 01-10: a retry unit restarted 3x after a finished run because its
    ExecStopPost hook ran past TimeoutStopSec). Two or more restarts while in auto-restart = a loop."""
    listing = run(["systemctl", "--user", "list-units", "--type=service", "--all", "--plain", "--no-legend",
                   "--state=auto-restart"], 15).stdout
    loops = []
    for line in listing.splitlines():
        unit = line.split()[0] if line.split() else ""
        if not unit.endswith(".service"):
            continue
        show = run(["systemctl", "--user", "show", unit, "-p", "NRestarts", "-p", "Result", "-p", "ExecMainStatus"],
                   15).stdout
        props = dict(l.split("=", 1) for l in show.splitlines() if "=" in l)
        if int(props.get("NRestarts") or 0) >= 2:
            loops.append(f"{unit} ({props.get('NRestarts')}x herstart, laatste: {props.get('Result')}, "
                         f"exit {props.get('ExecMainStatus')})")
    return [result("herstartlus", not loops, "; ".join(loops) if loops else "geen diensten in een herstartlus",
                   label="Herstartlus")]


def _meminfo() -> dict:
    out = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        out[k] = int(v.split()[0]) * 1024
    return out


def probe_resources():
    free = shutil.disk_usage("/").free
    out = [result("schijf", free >= DISK_MIN, f"{free / 1024 ** 3:.1f} GB vrij op /", label="Schijfruimte")]
    try:
        m = re.search(r"^some .*?avg300=([\d.]+)", Path("/proc/pressure/cpu").read_text(), re.M)
        cpu = float(m.group(1))
        out.append(result("cpu", cpu <= CPU_MAX, f"CPU-druk (some avg300) {cpu:.1f}%", label="CPU-druk"))
    except (OSError, AttributeError, ValueError):
        out.append(result("cpu", True, "/proc/pressure/cpu niet beschikbaar; overgeslagen", label="CPU-druk"))
    mem = _meminfo()
    swap = mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)
    out.append(result("swap", swap <= SWAP_MAX, f"{swap / 1024 ** 3:.2f} GB swap in gebruik", label="Swap"))
    avail = mem.get("MemAvailable", 0)
    out.append(result("geheugen", avail >= MEM_MIN, f"{avail / 1024 ** 2:.0f} MB geheugen beschikbaar",
                      label="Geheugen"))
    return out


def checkin_at():
    try:
        return float(json.loads(CHECKIN.read_text()).get("at", 0)) or None
    except (OSError, ValueError, AttributeError):
        return None


def probe_ops_checkin(now=None):
    now = now or time.time()
    at = checkin_at()
    if at is None:
        return [result("ops-checkin", False, "nog nooit ingecheckt", label="Ops-check-in")]
    age = now - at
    return [result("ops-checkin", age <= CHECKIN_MAX, f"laatste check-in {hhmm(at)} ({int(age // 60)} min geleden)",
                   label="Ops-check-in")]


def probe_werkmappen(now=None):
    r = werkmap_groei.growth_report(now=now)
    gb = werkmap_groei.gb
    kinds = {"werkmap": "werkmappen", "worktree": "worktrees", "wees": "wezen", "preview": "previews"}
    split = ", ".join(f"{kinds.get(k, k)} {gb(v)}" for k, v in sorted(r["per_soort"].items(), key=lambda kv: -kv[1]))
    big = ", ".join(f"{g['naam']} {gb(g['bytes'])}" for g in r["grootste"][:3])
    detail = (f"{r['resten']} resten ({gb(r['resten_bytes'])}; grens {werkmap_groei.LEFTOVER_MAX}), "
              f"totaal {gb(r['totaal'])} op schijf (grens {gb(werkmap_groei.TOTAL_MAX)}; los per soort: {split or '-'}); "
              f"grootste: {big or '-'}")
    return [result("werkmappen", r["ok"], detail, label="Werkmappen")]


PROBE_TIMEOUT = {"probe_werkmappen": 120}  # du over ~70 worktrees took 39 s alone, >55 s under load


def probes():
    extra = [probe_werkmappen] if WERKMAPPEN_AAN else []
    checkin = [probe_ops_checkin] if OPS_CHECKIN_AAN else []
    return [probe_model_login, probe_db, probe_pg_log, probe_git, probe_staging, probe_gateway,
            probe_dispatcher, probe_workers, probe_herstartlus, probe_resources] + checkin + extra


def collect() -> list:
    """All probes in parallel; a probe that crashes becomes one failing result (never takes the run down)."""
    results = []
    fns = probes()
    with ThreadPoolExecutor(max_workers=max(1, len(fns))) as pool:
        futures = [(fn, pool.submit(fn)) for fn in fns]
        for fn, fut in futures:
            try:
                results += fut.result(timeout=PROBE_TIMEOUT.get(fn.__name__, 55))
            except Exception as exc:  # noqa: BLE001 (report, don't die)
                name = fn.__name__.replace("probe_", "").replace("_", "-")
                results.append(result(name, False, f"probe-fout: {type(exc).__name__}: {redact(str(exc))[:150]}"))
    return results


# ------------------------------------------------------------------ routing
def log(line):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(f"{datetime.now(NL).isoformat(timespec='seconds')} {line}\n")


def _open_events(check):
    return {k: v for k, v in dp.events_matching(f"health:{check}:").items() if not v.get("resolved")}


def message_text(r, since):
    if r["blocking"]:
        return fc.text("gezondheid.blokkerend", label=r["label"], sinds=hhmm(since), detail=r["detail"])
    return fc.text("gezondheid.melding", label=r["label"], detail=r["detail"], sinds=hhmm(since))


def attention(results, now):
    reasons = [f"{r['check']}: {r['detail']}" for r in results if not r["ok"]]
    since = checkin_at() or 0
    fresh = sorted(p.name for p in INBOX.glob("*.md") if p.name != "LEESMIJ.md" and p.stat().st_mtime > since)
    if fresh:
        reasons.append("workflow-inbox: " + ", ".join(fresh))
    return reasons


def route(results, now, dry=False) -> dict:
    """Posts, edits and state; returns what it did (or would do, with ``dry``)."""
    try:
        prev = json.loads(STATE.read_text()).get("checks", {})
    except (OSError, ValueError):
        prev = {}
    chans = dp.channels()
    health_chan = chans.get("gezondheid")
    done = {"gepost": [], "opgelost": [], "alleen-log": []}
    checks = {}
    for r in results:
        key, old = r["check"], prev.get(r["check"]) or {}
        open_ev = _open_events(key)
        if r["ok"]:
            since = old.get("since") if old.get("ok") else now
            if old and not old.get("ok") and not dry:
                log(f"weer ok: {key}")
            for ev_key, info in open_ev.items():
                done["opgelost"].append(ev_key)
                if dry or not info.get("message"):
                    continue
                text = re.sub(r"^<@\d+>\s*", "", info.get("text", ""))
                try:
                    dp.edit(info["channel"], info["message"], fc.text("opgelost", tijd=hhmm(now), tekst=text[:1800]))
                except RuntimeError:
                    pass  # message already gone
                dp.event_mark(ev_key, resolved=now)
                log(f"opgelost: {ev_key}")
        else:
            if old and not old.get("ok"):
                since = old.get("since") or now
            elif open_ev:  # state file lost mid-episode: continue the open episode
                since = max(float(v.get("since", 0)) for v in open_ev.values()) or now
            else:
                since = now
            ev_key = f"health:{key}:{int(since)}"
            chan = chans.get("meldingen") if r["blocking"] else health_chan
            if not dp.event_seen(ev_key):
                text = message_text(r, since)
                if not chan:
                    if not old or old.get("ok"):
                        done["alleen-log"].append(key)
                        if not dry:
                            log(f"fout (geen #gezondheid-kanaal, alleen log): {key}: {r['detail']}")
                elif dry:
                    done["gepost"].append(f"{'#meldingen (ping)' if r['blocking'] else '#gezondheid'}: {text}")
                else:
                    msg = dp.send(chan, text, ping=r["blocking"], silent=not r["blocking"])
                    dp.event_mark(ev_key, message=msg["id"], channel=msg["channel_id"], text=msg["content"],
                                  check=key, since=since)
                    done["gepost"].append(ev_key)
                    log(f"gepost ({'meldingen' if r['blocking'] else 'gezondheid'}): {ev_key}: {r['detail']}")
        checks[key] = {"ok": r["ok"], "detail": r["detail"], "since": since, "last_run": now,
                       "blocking": r["blocking"], "label": r["label"]}
    reasons = attention(results, now)
    state = {"at": now, "ok": all(r["ok"] for r in results), "checks": checks}
    done["zelfcontrole"] = reasons
    if not dry:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
        tmp.replace(STATE)
        if reasons:
            FLAG.write_text(json.dumps({"at": now, "reasons": reasons}, ensure_ascii=False, indent=1))
        elif FLAG.exists():
            FLAG.unlink()
        bad = [r["check"] for r in results if not r["ok"]]
        log(f"{len(results) - len(bad)} ok, {len(bad)} fout" + (f": {', '.join(bad)}" if bad else ""))
    return done


def main():
    dry = "--dry-run" in sys.argv
    _lock = None if dry else dp.single_instance("hermes-health")  # noqa: F841 (held until exit)
    started = time.time()
    results = collect()
    now = time.time()
    done = route(results, now, dry=dry)
    if dry:
        for r in results:
            tag = "OK  " if r["ok"] else ("FOUT!" if r["blocking"] else "FOUT")
            print(f"{tag} {r['check']}: {r['detail']}")
        print(f"-- zou posten: {done['gepost'] or '-'}")
        print(f"-- zou alleen loggen (geen #gezondheid): {done['alleen-log'] or '-'}")
        print(f"-- zou oplossen: {done['opgelost'] or '-'}")
        print(f"-- zelfcontrole-nodig: {done['zelfcontrole'] or 'nee (vlagbestand weg)'}")
        print(f"-- duur {now - started:.1f} s")


if __name__ == "__main__":
    main()
