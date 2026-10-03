#!/usr/bin/env python3
"""Inventory of card work folders (scratch workspaces, git worktrees), the cleanup and the growth check.

  werkmap_groei.py [--dry-run]   the cleanup (Hermes cron "Kanban workspace cleanup"; was clean_kanban_workspaces.py)
  werkmap_groei.py --groei       the growth report as JSON

Used by the cleanup below (what may go) and by hermes-health.py
(check ``werkmappen`` via :func:`growth_report`). Everything here is read-only; the removing and
archiving helpers at the bottom only act when a cleanup script calls them in real mode.

Where things live:
  scratch workspaces  <HERMES_HOME>/kanban/boards/<board>/workspaces/<task_id> and the legacy
                      <HERMES_HOME>/kanban/workspaces/<task_id> (default board);
  git worktrees       <repo>/.worktrees/<task_id> for every repo in projects.db (project_folders), every
                      folder under the projects folder (flow config paden.projecten_map) and every repo a card's
                      workspace_path points into; the repo of the live Hermes install (~/.hermes/hermes-agent) and the
                      repos in opruimen.overslaan_repos are skipped entirely.

Card status comes from EVERY board DB (also archived boards and the legacy archive board
<HERMES_HOME>/kanban.db). A folder may go only when
  - all cards it belongs to (card id = folder name, plus cards whose workspace_path points at it) are
    done/archived for more than 24 h, or
  - none of those cards exists on any board any more and the folder's newest file is older than 7 days.
Any open card (todo/ready/running/review/blocked/triage/other) keeps it. Names that are not a card id,
``live-*`` and the names in opruimen.overslaan_namen are 'onbekend, overgeslagen'. Locked worktrees and folders that
are some process's working directory are kept too.

The cleanup (no-agent cron script, Hermes cron "Kanban workspace cleanup", deliver local): removes the work folders of
finished cards — scratch workspaces of every board and git worktrees under <repo>/.worktrees/<task_id>.

Rules:
  - only folders whose card(s) are done/archived for > 6 h (CLOSED_GRACE) on any board (team board, other boards and the
    legacy archive board ~/.hermes/kanban.db), or whose card exists nowhere any more and whose newest file is
    older than 7 days; an open card (todo/ready/running/review/blocked/triage) always keeps its folder;
  - never: the live Hermes repo and opruimen.overslaan_repos (whole repo), .worktrees/live-*, opruimen.overslaan_namen, locked worktrees,
    folders that are a process's working directory, names that are not a card id ('onbekend, overgeslagen');
  - git worktree: with changes, first `git diff HEAD --binary` → .patch and untracked files (without
    node_modules/.next/dist/build/__pycache__) → tar.gz in ~/hermes-archief-<YYYYMMDD>/werkmappen/ (dir 700,
    files 600) + an INDEX line (card, branch, commit); then `git worktree remove --force` + `git worktree prune`.
    Branches are never deleted; a detached HEAD with commits on no branch is skipped;
  - scratch workspace / orphan folder under .worktrees (no longer a registered worktree): tar.gz into the archive
    (without the regenerable folders) when it holds any other file, then remove.
Skips the whole run while a drain restart runs (~/.hermes/state/drain.json).
``--dry-run`` prints every decision and planned action and changes nothing.
Real mode logs to ~/.hermes/logs/clean_kanban_workspaces.log; stdout gets one summary line only when something
was removed or failed (empty stdout = nothing to do). The log and the output keep the old name
``clean_kanban_workspaces`` (CLEANUP_NAME).
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flow_config as fc  # noqa: E402  (git in agentrepo's: als de agent, vlag agent.gebruiker)
from flow_config import HOME, kanban  # noqa: E402

WORKSPACE = Path(os.environ.get("HERMES_WORKSPACE_PROJECTS") or fc.expand(fc.get("paden.projecten_map"), Path.home()))
ARCHIVE_ROOT = Path(os.environ.get("HERMES_ARCHIEF_ROOT") or fc.expand(fc.get("paden.archief_root"), Path.home()))
DRAIN = HOME / "state" / "drain.json"

DAY = 86400
CLOSED_GRACE = int(fc.get("drempels.werkmap_gesloten_na_uur") * 3600)  # done/archived for longer → may go
LEFTOVER_MISSING_AGE = DAY    # growth check: counts a card-less folder as leftover after 24 h
# Te laat (besluit eigenaar 03-10): langer dan bewaartermijn + marge dicht en nog niet opgeruimd; geen grens op aantal.
OVERDUE = CLOSED_GRACE + int(fc.get("drempels.werkmap_te_laat_marge_uur") * 3600)
TOTAL_MAX = int(fc.get("drempels.werkmappen_totaal_max_gb") * 1024 ** 3)
ARCHIVE_RESERVE = 5 * 1024 ** 3  # keep this much free on the archive disk after tarring
BACKUP_PREFIX = "hermes-backup/"  # fork patch 3b: uncommitted work secured as hermes-backup/<card>/<utc-ts>
BACKUP_KEEP = 7 * DAY             # such a branch goes once its card is done/archived for longer than this
BACKUP_STATE = HOME / "state" / "backup-branches.jsonl"

CLOSED = {"done", "archived"}
CARD_RE = re.compile(r"^t_[0-9a-f]{8}$")
SKIP_NAMES = set(fc.get("opruimen.overslaan_namen") or [])
SKIP_REPOS = set(fc.get("opruimen.overslaan_repos") or [])
REGENERABLE = {"node_modules", ".next", "dist", "build", "__pycache__"}  # never archived
CLEANUP_NAME = "clean_kanban_workspaces"  # log ~/.hermes/logs/<name>.log and the stdout prefix (unchanged since the merge)


# ------------------------------------------------------------------ cards
def board_dbs() -> list:
    """``[(board, db path)]`` for every board DB on disk, archived boards and the legacy archive included."""
    found, seen = [], set()
    candidates = [("default", HOME / "kanban.db")]
    root = HOME / "kanban" / "boards"
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        if d.is_dir():
            candidates.append((d.name, d / "kanban.db"))
    for board, db in candidates:
        if db.is_file() and db.resolve() not in seen:
            seen.add(db.resolve())
            found.append((board, db))
    return found


def _basename(path: str) -> str:
    return re.split(r"[\\/]", (path or "").rstrip("\\/"))[-1]


def load_cards() -> dict:
    """``{"cards": {id: {status, closed_at, boards}}, "by_dir": {folder name: {ids}}}``.

    A card on several boards counts as open when it is open anywhere; closed_at is the latest of completed_at,
    the last event and the last run end (conservative: the later, the longer it is kept)."""
    cards, by_dir = {}, {}
    for board, db in board_dbs():
        conn = kanban(db)
        try:
            last_event = dict(conn.execute("SELECT task_id, MAX(created_at) FROM task_events GROUP BY task_id"))
            last_run = dict(conn.execute("SELECT task_id, MAX(COALESCE(ended_at, started_at)) FROM task_runs "
                                         "GROUP BY task_id"))
            for t in conn.execute("SELECT id, status, completed_at, workspace_path FROM tasks"):
                closed = max(t["completed_at"] or 0, last_event.get(t["id"]) or 0, last_run.get(t["id"]) or 0)
                c = cards.setdefault(t["id"], {"status": t["status"], "closed_at": 0, "boards": []})
                c["boards"].append(board)
                if t["status"] not in CLOSED:
                    c["status"] = t["status"]
                elif c["status"] in CLOSED:
                    c["status"] = t["status"]
                c["closed_at"] = max(c["closed_at"], closed)
                if t["workspace_path"]:
                    by_dir.setdefault(_basename(t["workspace_path"]), set()).add(t["id"])
        except sqlite3.Error as exc:
            raise RuntimeError(f"bord {board} ({db}) niet leesbaar: {exc}") from exc
        finally:
            conn.close()
    return {"cards": cards, "by_dir": by_dir}


# ------------------------------------------------------------------ folders
def tree_stats(path: Path):
    """(bytes of files, newest mtime of any file or dir) without following symlinks."""
    total, newest = 0, 0.0
    try:
        newest = path.lstat().st_mtime
    except OSError:
        return 0, 0.0
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames + filenames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            newest = max(newest, st.st_mtime)
            if name in filenames:
                total += st.st_size
    return total, newest


def _skip_repo(repo: Path) -> bool:
    if repo.name in SKIP_REPOS:
        return True
    live = HOME / "hermes-agent"
    try:
        return live.exists() and str(live.resolve()).startswith(str(repo.resolve()) + os.sep)
    except OSError:
        return False


def repos() -> list:
    """Repo roots that may hold ``.worktrees``: projects.db folders, <projects folder>/*, and repos
    a card's workspace_path points into."""
    roots = set()
    db = HOME / "projects.db"
    if db.is_file():
        conn = kanban(db)
        try:
            for (p,) in conn.execute("SELECT path FROM project_folders"):
                roots.add(p)
            for (p,) in conn.execute("SELECT primary_path FROM projects WHERE primary_path IS NOT NULL"):
                roots.add(p)
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    if WORKSPACE.is_dir():
        roots |= {str(d) for d in WORKSPACE.iterdir() if d.is_dir()}
    for _board, dbp in board_dbs():
        conn = kanban(dbp)
        try:
            for (p,) in conn.execute("SELECT workspace_path FROM tasks WHERE workspace_path LIKE '%/.worktrees/%'"):
                roots.add(p.split("/.worktrees/")[0])
        finally:
            conn.close()
    out, seen = [], set()
    for r in sorted(roots):
        path = Path(r)
        try:
            if not (path / ".worktrees").is_dir():
                continue
            real = path.resolve()
        except OSError:  # e.g. /root/... from an old project entry
            continue
        if real in seen:
            continue
        seen.add(real)
        out.append(path)
    return out


def git(args, cwd, timeout=120):
    return subprocess.run(fc.als_agent_voor(cwd, ["git", "-C", str(cwd)] + args), capture_output=True, text=True,
                          timeout=timeout)


def registered_worktrees(repo: Path) -> dict:
    """``{realpath: {"branch", "head", "detached", "locked"}}`` from ``git worktree list --porcelain``."""
    r = git(["worktree", "list", "--porcelain"], repo, 60)
    out, cur = {}, None
    for line in r.stdout.splitlines() + [""]:
        if line.startswith("worktree "):
            cur = {"path": line[9:], "branch": "", "head": "", "detached": False, "locked": False}
        elif cur is None:
            continue
        elif line.startswith("HEAD "):
            cur["head"] = line[5:]
        elif line.startswith("branch "):
            cur["branch"] = line[7:].removeprefix("refs/heads/")
        elif line == "detached":
            cur["detached"] = True
        elif line.startswith("locked"):
            cur["locked"] = True
        elif line == "":
            out[os.path.realpath(cur["path"])] = cur
            cur = None
    return out


def busy_dirs() -> list:
    """Realpaths of the working directories of all processes we can see (a folder in use is never touched)."""
    found = []
    for pid in os.listdir("/proc") if os.path.isdir("/proc") else []:
        if pid.isdigit():
            try:
                found.append(os.path.realpath(os.readlink(f"/proc/{pid}/cwd")))
            except OSError:
                pass
    return found


def _in_use(path: Path, busy) -> bool:
    real = os.path.realpath(path)
    return any(b == real or b.startswith(real + os.sep) for b in busy)


def decide(ids, info, newest, now):
    """('weg' | 'blijft', reason) for a folder that belongs to card ids ``ids``."""
    cards = info["cards"]
    known = sorted(i for i in ids if i in cards)
    open_ = [i for i in known if cards[i]["status"] not in CLOSED]
    if open_:
        return "blijft", "kaart open: " + ", ".join(f"{i} ({cards[i]['status']})" for i in open_)
    if known:
        closed_at = max(cards[i]["closed_at"] for i in known)
        if now - closed_at > CLOSED_GRACE:
            days = (now - closed_at) / DAY
            return "weg", f"kaart {', '.join(known)} {cards[known[0]]['status']} sinds {days:.1f} dag"
        return "blijft", f"kaart {', '.join(known)} pas gesloten (< {CLOSED_GRACE // 3600} uur)"
    age = (now - newest) / DAY if newest else 0
    # Owner decision 30-09: only folders of done/archived cards are removed; a folder without a known card is reported only.
    return "blijft", f"geen bekende kaart (map {age:.1f} dag oud): alleen gemeld, niet opgeruimd"


def inventory(kinds=("werkmap", "worktree"), now=None, info=None) -> list:
    """One dict per folder: kind (werkmap | worktree | wees), path, name, card ids, size, newest,
    decision (weg | blijft | onbekend), reason; worktrees also repo, branch, head, detached."""
    now = now or time.time()
    info = info or load_cards()
    busy = busy_dirs()
    items = []

    def add(kind, path, ids, extra=None, *, card_named=True):
        size, newest = tree_stats(path)
        item = {"kind": kind, "path": path, "name": path.name, "cards": sorted(ids), "size": size,
                "newest": newest, **(extra or {})}
        if not card_named:
            item["decision"], item["reason"] = "onbekend", "naam is geen kaart-id, overgeslagen"
        elif _in_use(path, busy):
            item["decision"], item["reason"] = "blijft", "map in gebruik door een proces"
        else:
            item["decision"], item["reason"] = decide(ids, info, newest, now)
            item["closed_at"] = max((info["cards"][i]["closed_at"] for i in ids if i in info["cards"]), default=0)
        items.append(item)
        return item

    def card_named(name):
        return bool(CARD_RE.match(name)) or name in info["cards"]

    if "werkmap" in kinds:
        roots = [HOME / "kanban" / "workspaces"] + sorted((HOME / "kanban" / "boards").glob("*/workspaces"))
        for root in roots:
            for d in sorted(root.iterdir()) if root.is_dir() else []:
                if d.is_dir() and not d.is_symlink():
                    ids = {d.name} | info["by_dir"].get(d.name, set())
                    add("werkmap", d, ids, card_named=card_named(d.name))

    if "worktree" in kinds:
        for repo in repos():
            if _skip_repo(repo):
                continue
            wts = registered_worktrees(repo)
            for d in sorted((repo / ".worktrees").iterdir()):
                if not d.is_dir() or d.is_symlink():
                    continue  # stray files (logs) under .worktrees are not ours
                name = d.name
                reg = wts.get(os.path.realpath(d))
                extra = {"repo": repo}
                if reg:
                    extra.update(branch=reg["branch"], head=reg["head"], detached=reg["detached"])
                special = name.startswith("live-") or name in SKIP_NAMES
                ids = {name} | info["by_dir"].get(name, set())
                item = add("worktree" if reg else "wees", d, ids, extra,
                           card_named=card_named(name) and not special)
                if special:
                    item["reason"] = "vaste werkmap (live/patch), overgeslagen"
                elif reg and reg["locked"] and item["decision"] == "weg":
                    item["decision"], item["reason"] = "blijft", "worktree is gelocked"
    return items


def gb(n) -> str:
    return f"{n / 1024 ** 3:.1f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.0f} MB"


def growth_report(now=None, items=None) -> dict:
    """Totals for the health check: size of card workspaces + worktrees (live: real disk use, hardlinks
    once), the leftovers (folders of done/archived cards > CLOSED_GRACE, or card-less > 24 h), the overdue ones (closed
    longer than CLOSED_GRACE + margin and still there: the cleanup is not working) and the 5 largest."""
    live = items is None
    items = items if items is not None else inventory(now=now)
    per_kind = {}
    for i in items:
        per_kind[i["kind"]] = per_kind.get(i["kind"], 0) + i["size"]
    now = now or time.time()
    left = [i for i in items if i["decision"] == "weg" or (
        i.get("reason", "").startswith("geen bekende kaart") and i.get("newest") and now - i["newest"] > LEFTOVER_MISSING_AGE)]
    late = [i for i in items if i["decision"] == "weg" and i.get("closed_at") and now - i["closed_at"] > OVERDUE]
    largest = sorted(items, key=lambda i: -i["size"])[:5]
    total = sum(per_kind.values())
    if live and items:  # one du over all folders: pnpm hardlinks count once (per folder: 15.4 vs real 9.1 GB, 01-10)
        try:
            out = subprocess.run(["du", "-scb", *[str(i["path"]) for i in items]], capture_output=True, text=True,
                                 timeout=90).stdout.strip().splitlines()
            total = int(out[-1].split()[0]) if out else total
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
    return {
        "totaal": total,
        "per_soort": per_kind,
        "resten": len(left),
        "resten_bytes": sum(i["size"] for i in left),
        "onbekend": sum(1 for i in items if i["decision"] == "onbekend"),
        "grootste": [{"pad": str(i["path"]), "naam": i["name"], "soort": i["kind"], "bytes": i["size"],
                      "besluit": i["decision"]} for i in largest],
        "te_laat": len(late),
        "ok": not late and total <= TOTAL_MAX,
    }


# ------------------------------------------------------------------ acting (cleanup scripts only)
def drain_running() -> bool:
    return DRAIN.exists()


def archive_dir(now=None) -> Path:
    base = ARCHIVE_ROOT / f"hermes-archief-{fc.tijd(now or None, '%Y%m%d')}"
    d = base / "werkmappen"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(base, 0o700)
    os.chmod(d, 0o700)
    return d


def _private(path: Path):
    os.chmod(path, 0o600)


def index_line(adir: Path, **fields):
    line = "\t".join([fc.nu().isoformat(timespec="seconds")] + [f"{k}={v}" for k, v in fields.items()])
    idx = adir / "INDEX"
    with idx.open("a") as fh:
        fh.write(line + "\n")
    _private(idx)


def _stamp(item) -> str:
    repo = item.get("repo")
    return f"{item['kind']}-{repo.name + '-' if repo else ''}{item['name']}-{fc.tijd(None, '%H%M%S')}"


def _room_for(adir: Path, size: int) -> bool:
    return shutil.disk_usage(adir).free - size > ARCHIVE_RESERVE


def _regenerable(rel: str) -> bool:
    return any(part in REGENERABLE for part in Path(rel).parts)


def has_keepable_files(path: Path) -> bool:
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if d not in REGENERABLE]
        if filenames:
            return True
    return False


def tar_dir(path: Path, dest: Path):
    """tar.gz of ``path`` (symlinks stored as links, regenerable folders left out), verified by reading it back;
    chmod 600."""
    def flt(ti):
        if any(p in REGENERABLE for p in Path(ti.name).parts[1:]):
            return None
        return ti
    with tarfile.open(dest, "w:gz") as tf:
        tf.add(str(path), arcname=path.name, filter=flt)
    _private(dest)
    with tarfile.open(dest, "r:gz") as tf:
        for _m in tf:
            pass


def remove_plain(item, adir: Path, *, want_tar: bool) -> str:
    """Archive (when ``want_tar``) and remove a plain folder; returns what was done."""
    path = item["path"]
    note = "zonder archief"
    if want_tar:
        if not _room_for(adir, item["size"]):
            raise RuntimeError("te weinig schijfruimte voor het archief")
        dest = adir / f"{_stamp(item)}.tar.gz"
        tar_dir(path, dest)
        note = f"archief {dest.name}"
    shutil.rmtree(path)
    index_line(adir, soort=item["kind"], kaart=",".join(item["cards"]) or "-", pad=path, actie=note)
    return note


def remove_worktree(item, adir: Path) -> str:
    """Save uncommitted changes (patch + untracked tar.gz), then ``git worktree remove --force`` + prune.
    The branch stays. A detached HEAD with commits on no branch/tag/remote is refused."""
    wt, repo = item["path"], item["repo"]
    st = git(["--no-optional-locks", "status", "--porcelain"], wt)
    if st.returncode != 0:
        raise RuntimeError(f"git status faalt: {st.stderr.strip()[:120]}")
    if item.get("detached"):
        loose = git(["rev-list", "--count", "HEAD", "--not", "--branches", "--tags", "--remotes"], wt)
        if loose.returncode != 0 or loose.stdout.strip() != "0":
            raise RuntimeError("losse HEAD met commits die op geen branch staan")
    saved = []
    if st.stdout.strip():
        stamp = _stamp(item)
        if not _room_for(adir, item["size"]):
            raise RuntimeError("te weinig schijfruimte voor het archief")
        diff = subprocess.run(fc.als_agent_voor(wt, ["git", "-C", str(wt), "diff", "HEAD", "--binary"]), capture_output=True,
                              timeout=120)
        if diff.returncode != 0:
            raise RuntimeError("git diff faalt")
        if diff.stdout:
            p = adir / f"{stamp}.patch"
            p.write_bytes(diff.stdout)
            _private(p)
            saved.append(p.name)
        ls = subprocess.run(fc.als_agent_voor(wt, ["git", "-C", str(wt), "ls-files", "--others", "--exclude-standard", "-z"]),
                            capture_output=True, timeout=120)
        if ls.returncode != 0:
            raise RuntimeError("git ls-files faalt")
        untracked = [f for f in ls.stdout.decode(errors="surrogateescape").split("\0") if f and not _regenerable(f)]
        if untracked:
            t = adir / f"{stamp}-untracked.tar.gz"
            with tarfile.open(t, "w:gz") as tf:
                for rel in untracked:
                    tf.add(str(wt / rel), arcname=rel, recursive=False)
            _private(t)
            with tarfile.open(t, "r:gz") as tf:
                for _m in tf:
                    pass
            saved.append(t.name)
    rm = git(["worktree", "remove", "--force", str(wt)], repo)
    if rm.returncode != 0:
        raise RuntimeError(f"git worktree remove faalt: {rm.stderr.strip()[:120]}")
    git(["worktree", "prune"], repo)
    index_line(adir, soort="worktree", kaart=",".join(item["cards"]) or "-", repo=repo.name,
               branch=item.get("branch") or "(los)", commit=item.get("head", "")[:12],
               bewaard="+".join(saved) or "geen wijzigingen")
    return "wijzigingen bewaard: " + ", ".join(saved) if saved else "schoon"


def backup_branches(info=None, now=None) -> list:
    """Backup branches of fork patch 3b in every project repo, each with a decision:
    ``weg`` when its card is done/archived for longer than BACKUP_KEEP, else ``blijft`` (open card, recently
    closed, or a card that exists nowhere: those are only reported)."""
    info = info or load_cards()
    now = now or time.time()
    out = []
    for repo in repos():
        r = git(["for-each-ref", "--format=%(refname:short) %(objectname)", f"refs/heads/{BACKUP_PREFIX}"], repo)
        for line in (r.stdout or "").splitlines():
            ref, _, sha = line.strip().partition(" ")
            parts = ref.split("/")
            card = parts[1] if len(parts) >= 3 else ""
            c = info["cards"].get(card)
            if c and c["status"] in CLOSED and now - c["closed_at"] > BACKUP_KEEP:
                decision, why = "weg", f"kaart {card} {c['status']} sinds {(now - c['closed_at']) / DAY:.1f} dag"
            elif c and c["status"] in CLOSED:
                decision, why = "blijft", f"kaart {card} pas gesloten (< {BACKUP_KEEP // DAY} dagen)"
            elif c:
                decision, why = "blijft", f"kaart {card} open ({c['status']})"
            else:
                decision, why = "blijft", "geen bekende kaart: alleen gemeld"
            out.append({"repo": repo, "ref": ref, "sha": sha, "card": card, "decision": decision, "reason": why})
    return out


def remove_backup(b, adir: Path) -> str:
    """Archive the backup commit as a patch (600) with an INDEX line, then delete the branch."""
    patch = adir / f"backup-{b['ref'].replace('/', '_')}.patch"
    r = subprocess.run(fc.als_agent_voor(b["repo"], ["git", "-C", str(b["repo"]), "format-patch", "-1", "--stdout", "--binary", b["sha"]]),
                       capture_output=True, timeout=120)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError(f"format-patch mislukt: {r.stderr.decode(errors='replace')[-200:]}")
    patch.write_bytes(r.stdout)
    _private(patch)
    index_line(adir, soort="back-upbranch", kaart=b["card"], pad=f"{b['repo']}:{b['ref']}", commit=b["sha"][:12],
               actie=f"archief {patch.name}")
    d = git(["branch", "-D", b["ref"]], b["repo"])
    if d.returncode != 0:
        raise RuntimeError(f"branch -D mislukt: {(d.stderr or '').strip()[-200:]}")
    return f"gearchiveerd als {patch.name}, branch weg"


def record_backups(gone: int, kept: int, failed: int, now=None):
    BACKUP_STATE.parent.mkdir(parents=True, exist_ok=True)
    with BACKUP_STATE.open("a") as fh:
        fh.write(json.dumps({"at": now or time.time(), "weg": gone, "bewaard": kept, "fout": failed}) + "\n")


def backup_summary(since: float):
    """(removed since ``since``, kept at the last run) from BACKUP_STATE, or None without data."""
    try:
        rows = [json.loads(l) for l in BACKUP_STATE.read_text().splitlines() if l.strip()]
    except (OSError, ValueError):
        return None
    if not rows:
        return None
    return sum(r.get("weg", 0) for r in rows if r.get("at", 0) > since), rows[-1].get("bewaard", 0)


def plan(item) -> str:
    """What a real run would do with a 'weg' item (read-only; used by --dry-run and the log)."""
    if item["kind"] == "worktree":
        st = git(["--no-optional-locks", "status", "--porcelain"], item["path"])
        if st.returncode != 0:
            return "zou overslaan: git status faalt"
        if item.get("detached"):
            loose = git(["rev-list", "--count", "HEAD", "--not", "--branches", "--tags", "--remotes"], item["path"])
            if loose.returncode != 0 or loose.stdout.strip() != "0":
                return "zou overslaan: losse HEAD met commits die op geen branch staan"
        n = len(st.stdout.splitlines())
        keep = f"eerst {n} wijziging(en) bewaren (patch + untracked tar.gz), " if n else ""
        return f"{keep}git worktree remove --force + prune (branch {item.get('branch') or '(los)'} blijft)"
    if has_keepable_files(item["path"]):
        return "tar.gz naar archief, dan map weg"
    return "map weg (geen archief nodig)"


def run_cleanup(script: str, kinds, argv) -> int:
    """Main of the cleanup (``werkmap_groei.py [--dry-run]``)."""
    dry = "--dry-run" in argv
    log = Logger(script, dry)
    if drain_running():
        if not dry:
            log("overgeslagen: drain-herstart loopt (state/drain.json)")
            print(f"{script}: overgeslagen, drain-herstart loopt")
            return 0
        print("LET OP: state/drain.json bestaat; een echte run zou nu niets doen.")
    import discord_post as dp
    _lock = None if dry else dp.single_instance("werkmappen-opruimen")  # noqa: F841 (held until exit)
    now = time.time()
    items = inventory(kinds, now=now)
    gone = [i for i in items if i["decision"] == "weg"]
    unknown = [i for i in items if i["decision"] == "onbekend"]
    kept = [i for i in items if i["decision"] == "blijft"]
    if dry:
        print(f"== {script} --dry-run: {len(gone)} weg ({gb(sum(i['size'] for i in gone))}), "
              f"{len(kept)} blijven, {len(unknown)} onbekend")
        for i in gone:
            report_items([i], log)
            print(f"         → {plan(i)}")
        for i in unknown + kept:
            report_items([i], log)
        if "worktree" in kinds:
            for b in backup_branches(load_cards(), now):
                print(f"{b['decision']:<8} back-upbranch {b['repo'].name}:{b['ref']} — {b['reason']}")
        return 0
    adir = archive_dir(now) if gone else None
    done, archived, failed, freed = 0, 0, [], 0
    for i in gone:
        if drain_running():
            log("gestopt: drain-herstart begonnen")
            failed.append(f"{i['name']} (drain)")
            break
        fresh = load_cards()  # the card may have been reopened while earlier items were archived
        ids = set(i["cards"]) | fresh["by_dir"].get(i["name"], set())
        verdict, why = decide(ids, fresh, i["newest"], time.time())
        if verdict != "weg" or _in_use(i["path"], busy_dirs()):
            log(f"blijft alsnog: {i['path']} ({why})")
            continue
        try:
            if i["kind"] == "worktree":
                note = remove_worktree(i, adir)
                archived += note.startswith("wijzigingen")
            else:
                note = remove_plain(i, adir, want_tar=has_keepable_files(i["path"]))
                archived += note.startswith("archief")
            done += 1
            freed += i["size"]
            log(f"weg: {i['path']} [{i['kind']}, {gb(i['size'])}] — {i['reason']} — {note}")
        except Exception as exc:  # noqa: BLE001 (one folder failing never stops the rest)
            failed.append(i["name"])
            log(f"FOUT: {i['path']}: {type(exc).__name__}: {exc}")
    for i in unknown:
        log(f"onbekend, overgeslagen: {i['path']}")
    b_done = b_kept = 0
    if "worktree" in kinds:  # backup branches of fork patch 3b (owner decision: away 7 days after the card closed)
        for b in backup_branches(load_cards(), time.time()):
            if b["decision"] != "weg":
                b_kept += 1
                continue
            try:
                adir = adir or archive_dir(now)
                log(f"weg: back-upbranch {b['repo']}:{b['ref']} — {b['reason']} — {remove_backup(b, adir)}")
                b_done += 1
            except Exception as exc:  # noqa: BLE001
                failed.append(b["ref"])
                log(f"FOUT: back-upbranch {b['ref']}: {type(exc).__name__}: {exc}")
        record_backups(b_done, b_kept, sum(1 for f in failed if f.startswith(BACKUP_PREFIX)), now)
    if done or failed or b_done:
        parts = [f"{done} weg ({gb(freed)})"] + ([f"{b_done} back-upbranch(es) weg"] if b_done else [])
        if archived:
            parts.append(f"{archived} gearchiveerd in {adir}")
        if unknown:
            parts.append(f"{len(unknown)} onbekend overgeslagen")
        if failed:
            parts.append(f"{len(failed)} fout: {', '.join(failed[:5])} (zie {log.path})")
        print(f"{script}: " + "; ".join(parts))
    return 1 if failed else 0


class Logger:
    def __init__(self, script: str, dry: bool):
        self.path = HOME / "logs" / f"{script}.log"
        self.dry = dry

    def __call__(self, line: str):
        fc.log(self.path, line, echo=self.dry, schrijf=not self.dry)


def report_items(items, log):
    for i in items:
        where = f"{i['repo'].name}/.worktrees/{i['name']}" if i.get("repo") else str(i["path"])
        log(f"{i['decision']:8} {i['kind']:8} {gb(i['size']):>8}  {where}  — {i['reason']}")


if __name__ == "__main__":
    if "--groei" in sys.argv[1:]:
        print(json.dumps(growth_report(), indent=1, ensure_ascii=False))
    else:
        sys.exit(run_cleanup(CLEANUP_NAME, ("werkmap", "worktree"), sys.argv[1:]))
