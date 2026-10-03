#!/usr/bin/env python3
"""scan_personal.py [opties] <pad>… : find personal data and secrets before anything is shared.

Exit 0 = clean, 1 = findings (each as "<file>:<line>: <soort>: <wat>"), 2 = usage error. A secret value is
never printed: only where it was found and which variable it belongs to.

What it looks for (all case-insensitive where that makes sense):
  generic      Discord snowflake IDs (17–20 digits), Discord bot tokens, GitHub tokens (gh*_ / github_pat_),
               sk-/xox*-/AKIA keys, "api_key = <long value>" style assignments, e-mail addresses (except
               @example.invalid / @example.com / noreply@…), /home/<user> paths, app-hosting domains (Railway,
               Vercel, Fly, Heroku, ngrok; see GENERIC) and every URL host outside a small allowlist, model names
               (see MODEL_NAMES).
  this machine the current user name and host name (whole word), every value (>= 8 chars) in
               $HERMES_HOME/.env and profiles/*/.env (compared in memory), and string values in auth.json.
  local config (flow_config): the owner's name, project slugs, names, repo folder names and staging hosts,
               the project files in team/projects/, the Discord guild/owner/channel IDs, and export.prive_termen.
Allowlist: export.allowlist in the flow config and/or ``--allowlist <file>`` (lines "<path glob>\t<regex>\t# why";
the regex is matched against the finding's text). Every allowlist hit is reported with --toon-allowlist.

Options:
  --git <repo>       also scan the full git history of <repo>: every blob of every commit, commit messages,
                     author and committer names/e-mails
  --allowlist <f>    extra allowlist file (see above)
  --geen-lokaal      skip the checks that read this machine's .env/config (only the generic patterns)
  --json             machine-readable output
  --toon-allowlist   also list the findings that the allowlist allowed
"""
import fnmatch
import getpass
import json
import re
import socket
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Model names, stored reversed so this file does not flag itself.
MODEL_NAMES = [w[::-1] for w in ("supo", "tennos", "kraps", "esum", "xedoc", "ukiah")]
MODEL_PREFIXES = [w[::-1] for w in ("-tpg",)]
OK_HOSTS = {"discord.com", "www.discord.com", "discord.gg", "github.com", "www.github.com", "docs.github.com",
            "raw.githubusercontent.com", "example.invalid", "example.com", "www.example.com", "localhost",
            "127.0.0.1", "docs.python.org", "pypi.org", "www.python.org", "python.org", "pyyaml.org",
            "opensource.org", "keepachangelog.com", "mermaid.js.org", "support.discord.com",
            "discord.dev", "discordpy.readthedocs.io", "systemd.io", "www.freedesktop.org", "gitleaks.io"}
OK_USERS = {"root", "user", "ubuntu", "admin", "debian", "pi", "runner", "test", "hermes"}
GENERIC = [
    ("discord-id", re.compile(r"(?<![0-9A-Za-z])[0-9]{17,20}(?![0-9])")),
    ("discord-token", re.compile(r"[MNO][A-Za-z\d_-]{23,27}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,}")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("api-key", re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})")),
    ("secret-toewijzing", re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password|passwd)\s*[:=]\s*['\"]?"
                                     r"(?![<{$])[A-Za-z0-9_\-/+=]{16,}")),
    ("e-mail", re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),  # not "+@pytest.fixture" in a diff
    ("home-pad", re.compile(r"/home/[A-Za-z0-9_][A-Za-z0-9_.-]*")),
    ("hosting-domein", re.compile(r"(?i)\b[a-z0-9.-]+\.(?:up\.railway\.app|railway\.app|vercel\.app|fly\.dev|"
                                  r"herokuapp\.com|ngrok(?:-free)?\.(?:app|io|dev))\b")),
    ("url-host", re.compile(r"(?i)\bhttps?://([a-z0-9.-]+\.[a-z]{2,}|localhost|127\.0\.0\.1)(?::\d+)?")),
    ("model-naam", re.compile(r"(?i)(?<![a-z0-9_])(?:" + "|".join(map(re.escape, MODEL_NAMES)) + r")(?![a-z0-9_])|"
                              r"(?<![a-z0-9_])(?:" + "|".join(map(re.escape, MODEL_PREFIXES)) + r")\d")),
]
OK_EMAIL = re.compile(r"(?i)(@example\.(invalid|com)$|^noreply@)")


def local_terms():
    """``(terms, secrets)``: personal terms of this machine/config (shown) and secret values (never shown)."""
    terms, secrets = {}, {}
    user = getpass.getuser()
    if len(user) >= 3 and user.lower() not in OK_USERS:
        terms[user] = "gebruikersnaam"
    host = socket.gethostname().split(".")[0]
    if len(host) >= 4 and host.lower() not in ("localhost",):
        terms[host] = "hostnaam"
    try:
        import flow_config as fc
    except Exception:  # noqa: BLE001
        return terms, secrets
    home = fc.HOME
    for env in [home / ".env", *sorted(home.glob("profiles/*/.env"))]:
        try:
            for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    v = v.strip().strip("'\"")
                    if len(v) >= 8 and not v.lower() in ("true", "false") and not re.fullmatch(r"[0-9.]+", v):
                        secrets[v] = f"waarde van {k.strip()} uit {env.name}"
        except OSError:
            pass
    try:
        def walk(x, where):
            if isinstance(x, dict):
                for k, v in x.items():
                    walk(v, f"{where}.{k}")
            elif isinstance(x, list):
                for v in x:
                    walk(v, where)
            elif isinstance(x, str) and len(x) >= 12 and not x.startswith(("http", "/")):
                secrets[x] = f"waarde uit auth.json ({where.split('.')[-1]})"
        walk(json.loads((home / "auth.json").read_text()), "auth")
    except (OSError, ValueError):
        pass
    try:
        if fc.config_path() is None:
            raise LookupError
        owner = fc.owner_name()
        if owner.lower() not in ("de eigenaar", "eigenaar", "the owner", "owner"):
            terms[owner] = "eigenaar"
        for slug, p in fc.projects().items():
            if slug == "voorbeeld-project":
                continue
            for t in (slug, p.get("naam"), p["repo"].name if p.get("repo") else None):
                if t and len(str(t)) >= 4:
                    terms[str(t)] = "project uit de config"
            m = re.match(r"https?://([^/]+)", str(p.get("staging_url") or ""))
            if m:
                terms[m.group(1)] = "staging-domein"
        for k, v in fc.channel_ids().items():
            if re.fullmatch(r"\d{6,}", str(v)):
                terms[str(v)] = f"Discord-ID ({k})"
        for f in (home / "team" / "projects").glob("*.md"):
            if not f.name.startswith("_") and len(f.stem) >= 4:
                terms[f.stem] = "projectbestand"
        for t in fc.get("export.prive_termen") or []:
            terms[str(t)] = "prive-term"
        for f in (home / "team" / "deployments").glob("*.json"):  # Railway-ID's van elk project, zonder handwerk
            for v in json.loads(f.read_text()).values():
                if re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", str(v)):
                    terms[str(v)] = f"Railway-ID ({f.stem})"
    except LookupError:
        pass
    return terms, secrets


def load_allowlist(path):
    rules = []
    try:
        import flow_config as fc
        for entry in fc.get("export.allowlist") or []:
            glob, _, rx = str(entry).partition(":")
            rules.append((glob or "*", re.compile(rx), "config"))
    except Exception:  # noqa: BLE001
        pass
    if path:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                raise SystemExit(f"allowlist {path}: regel zonder tab: {line!r}")
            rules.append((parts[0].strip(), re.compile(parts[1].strip()), (parts[2].strip() if len(parts) > 2 else "")))
    return rules


class Scanner:
    def __init__(self, local=True, allow=()):
        self.terms, self.secrets = local_terms() if local else ({}, {})
        self.term_rx = [(re.compile(r"(?i)(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])"), t, why)
                        for t, why in sorted(self.terms.items(), key=lambda kv: -len(kv[0]))]
        self.allow = list(allow)
        self.findings, self.allowed = [], []

    def _add(self, where, line_no, kind, what, shown):
        hit = {"bestand": where, "regel": line_no, "soort": kind, "wat": shown}
        path = where.split(":", 2)[-1] if where.startswith("git:") else where  # history: match on the file path
        for glob, rx, why in self.allow:
            if fnmatch.fnmatch(path, glob) and rx.search(what):
                self.allowed.append({**hit, "allowlist": why or rx.pattern})
                return
        self.findings.append(hit)

    def scan_text(self, where, text):
        for n, line in enumerate(text.splitlines(), 1):
            for kind, rx in GENERIC:
                for m in rx.finditer(line):
                    what = m.group(0)
                    if kind == "e-mail" and OK_EMAIL.search(what):
                        continue
                    if kind == "url-host":
                        host = m.group(1).lower()
                        if host in OK_HOSTS or host.endswith(".example.invalid"):
                            continue
                        what = host
                    if kind in ("discord-token", "github-token", "api-key", "secret-toewijzing"):
                        self._add(where, n, kind, what, f"{kind} (waarde niet getoond)")
                    else:
                        self._add(where, n, kind, what, what)
            for rx, term, why in self.term_rx:
                if rx.search(line):
                    self._add(where, n, "persoonlijk", term, f"{why}: {term}")
            for value, why in self.secrets.items():
                if value in line:
                    self._add(where, n, "geheim", why, f"geheim ({why}; waarde niet getoond)")

    def scan_path(self, root: Path):
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts)
        for f in files:
            rel = str(f.relative_to(root)) if root.is_dir() else f.name
            self.scan_text(f"{rel} (bestandsnaam)", rel)
            data = f.read_bytes()
            if b"\0" in data[:4096]:
                continue  # binary
            self.scan_text(rel, data.decode("utf-8", errors="replace"))

    def scan_git(self, repo: Path):
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout
        commits = git("rev-list", "--all").split()
        seen = set()
        for c in commits:
            meta = git("show", "-s", "--format=%an <%ae>%n%cn <%ce>%n%B", c)
            self.scan_text(f"git:{c[:10]} (bericht/auteur)", meta)
            for line in git("ls-tree", "-r", c).splitlines():
                info, path = line.split("\t", 1)
                _mode, kind, blob = info.split()
                if kind != "blob" or blob in seen:
                    continue
                seen.add(blob)
                data = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", blob], capture_output=True,
                                      check=True).stdout
                if b"\0" in data[:4096]:
                    continue
                self.scan_text(f"git:{c[:10]}:{path}", data.decode("utf-8", errors="replace"))
        return len(commits)


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    opts, paths, i = {"git": [], "allowlist": None}, [], 0
    flags = set()
    while i < len(argv):
        a = argv[i]
        if a == "--git":
            opts["git"].append(Path(argv[i + 1]))
            i += 2
        elif a == "--allowlist":
            opts["allowlist"] = argv[i + 1]
            i += 2
        elif a.startswith("--"):
            flags.add(a)
            i += 1
        else:
            paths.append(Path(a))
            i += 1
    s = Scanner(local="--geen-lokaal" not in flags, allow=load_allowlist(opts["allowlist"]))
    for p in paths:
        if not p.exists():
            print(f"bestaat niet: {p}", file=sys.stderr)
            return 2
        s.scan_path(p)
    n_commits = sum(s.scan_git(r) for r in opts["git"])
    if "--json" in flags:
        print(json.dumps({"vondsten": s.findings, "allowlist": s.allowed, "commits": n_commits}, ensure_ascii=False,
                         indent=1))
    else:
        for f in s.findings:
            print(f"{f['bestand']}:{f['regel']}: {f['soort']}: {f['wat']}")
        if "--toon-allowlist" in flags:
            for f in s.allowed:
                print(f"(toegestaan) {f['bestand']}:{f['regel']}: {f['soort']}: {f['wat']} — {f['allowlist']}")
        extra = f", {n_commits} commit(s) in de geschiedenis" if opts["git"] else ""
        print(f"scan: {len(s.findings)} vondst(en), {len(s.allowed)} toegestaan via allowlist{extra}; "
              f"{len(s.terms)} lokale termen en {len(s.secrets)} geheime waarden vergeleken")
    return 1 if s.findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
