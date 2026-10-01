#!/usr/bin/env python3
"""Shared read-only helpers for the team cron scripts: active projects and their Kanban cards.

Active project = a file in ~/.hermes/team/projects/ (not _TEMPLATE.md) with a line
"STATUS: ACTIEF"; no STATUS line means not active. It is linked to Kanban via the projects.db slug (= file name):
cards match on project_id or on a workspace path under the project's folder.

Boards: every non-archived board under ~/.hermes/kanban/boards/ (the same set the dispatcher walks),
except ``default``, which is the archive of the old setup.
"""
import json
import os
import re
import sqlite3
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
PROJECTS_DIR = HOME / "team" / "projects"


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def active_projects(statuses=("ACTIEF",)) -> dict:
    """``{slug: {"name", "ids", "paths", "status", "file"}}`` for every project file whose STATUS is in ``statuses``."""
    slugs = {}
    wanted = "|".join(re.escape(s) for s in statuses)
    for f in sorted(PROJECTS_DIR.glob("*.md")):
        if f.name.startswith("_"):
            continue
        m = re.search(rf"^\s*STATUS:\s*({wanted})\s*$", f.read_text(encoding="utf-8", errors="replace"), re.M | re.I)
        if not m:
            continue
        slugs[f.stem] = {"name": f.stem, "ids": set(), "paths": [], "status": m.group(1).upper(), "file": f}
    db = HOME / "projects.db"
    if slugs and db.exists():
        conn = _ro(db)
        for row in conn.execute("SELECT p.slug, p.id, p.name, f.path FROM projects p "
                                "LEFT JOIN project_folders f ON f.project_id = p.id"):
            if row["slug"] in slugs:
                entry = slugs[row["slug"]]
                entry["name"] = row["name"] or entry["name"]
                entry["ids"].add(row["id"])
                if row["path"]:
                    entry["paths"].append(row["path"].rstrip("/") + "/")
    return slugs


def project_of(task: sqlite3.Row, projects: dict):
    """Slug of the active project a card belongs to, else None."""
    workspace = (task["workspace_path"] or "").rstrip("/") + "/"
    for slug, p in projects.items():
        if task["project_id"] in p["ids"] or any(workspace.startswith(path) for path in p["paths"]):
            return slug
    return None


def boards() -> list:
    """``[(slug, kanban.db path)]`` for every non-archived board except ``default``."""
    found = []
    root = HOME / "kanban" / "boards"
    for d in sorted(root.iterdir(), key=lambda p: p.name.lower()) if root.is_dir() else []:
        if not d.is_dir() or d.name.startswith("_") or d.name == "default":
            continue
        if not ((d / "board.json").exists() or (d / "kanban.db").exists()):
            continue
        try:
            meta = json.loads((d / "board.json").read_text(encoding="utf-8")) if (d / "board.json").exists() else {}
        except (OSError, ValueError):
            meta = {}
        if meta.get("archived") or not (d / "kanban.db").exists():
            continue
        found.append((d.name, d / "kanban.db"))
    return found


def kanban(db_path: Path) -> sqlite3.Connection:
    return _ro(db_path)
