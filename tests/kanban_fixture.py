"""A scratch Hermes kanban board and project DB for the tests (the columns the flow scripts read, same names and
types as Hermes 0.21.4). Nothing here talks to a real Hermes install."""
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT, status TEXT NOT NULL,
    priority INTEGER DEFAULT 0, created_by TEXT, created_at INTEGER NOT NULL, started_at INTEGER,
    completed_at INTEGER, workspace_kind TEXT NOT NULL DEFAULT 'scratch', workspace_path TEXT, branch_name TEXT,
    project_id TEXT, result TEXT, idempotency_key TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_failure_error TEXT, last_heartbeat_at INTEGER, current_run_id INTEGER, model_override TEXT,
    block_kind TEXT, block_recurrences INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, run_id INTEGER, kind TEXT NOT NULL, payload TEXT,
    created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, profile TEXT, step_key TEXT, status TEXT NOT NULL,
    started_at INTEGER NOT NULL, ended_at INTEGER, outcome TEXT, summary TEXT, metadata TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS task_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, author TEXT NOT NULL, body TEXT NOT NULL,
    created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS task_links (parent_id TEXT NOT NULL, child_id TEXT NOT NULL, PRIMARY KEY (parent_id, child_id));
CREATE TABLE IF NOT EXISTS kanban_notify_subs (
    task_id TEXT NOT NULL, platform TEXT NOT NULL, chat_id TEXT NOT NULL, thread_id TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL, PRIMARY KEY (task_id, platform, chat_id, thread_id));
"""
PROJECTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    description TEXT, board_slug TEXT, primary_path TEXT, created_at INTEGER NOT NULL, archived INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS project_folders (project_id TEXT NOT NULL, path TEXT NOT NULL, label TEXT,
    is_primary INTEGER NOT NULL DEFAULT 0, added_at INTEGER NOT NULL, PRIMARY KEY (project_id, path));
"""


def board_db(home: Path, board="team") -> Path:
    d = Path(home) / "kanban" / "boards" / board
    d.mkdir(parents=True, exist_ok=True)
    (d / "board.json").write_text(json.dumps({"slug": board, "archived": False}))
    db = d / "kanban.db"
    with sqlite3.connect(db) as c:
        c.executescript(SCHEMA)
    return db


def add_project(home: Path, slug, name, path, status="ACTIEF", staging_line=""):
    home = Path(home)
    with sqlite3.connect(home / "projects.db") as c:
        c.executescript(PROJECTS_SCHEMA)
        pid = f"p_{slug[:8]}"
        c.execute("INSERT OR REPLACE INTO projects (id, slug, name, primary_path, created_at) VALUES (?,?,?,?,?)",
                  (pid, slug, name, str(path), int(time.time())))
        c.execute("INSERT OR REPLACE INTO project_folders (project_id, path, is_primary, added_at) VALUES (?,?,1,?)",
                  (pid, str(path), int(time.time())))
    pdir = home / "team" / "projects"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / f"{slug}.md").write_text(f"STATUS: {status}\n\n# {name}\n\n{staging_line}\n")
    return pid


def conn(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def add_card(db, task_id, title, status="todo", assignee=None, project_id=None, body="", created_ago=7200,
             completed_ago=None):
    now = int(time.time())
    with conn(db) as c:
        c.execute("INSERT INTO tasks (id, title, body, assignee, status, created_at, completed_at, project_id) "
                  "VALUES (?,?,?,?,?,?,?,?)", (task_id, title, body, assignee, status, now - created_ago,
                                               now - completed_ago if completed_ago is not None else None, project_id))
        c.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'created', NULL, ?)",
                  (task_id, now - created_ago))


def block(db, task_id, reason, kind=None, ago=0):
    """Block a card (a new 'blocked' event with the reason, like kanban_block)."""
    now = int(time.time()) - ago
    with conn(db) as c:
        c.execute("UPDATE tasks SET status = 'blocked', block_kind = COALESCE(?, block_kind) WHERE id = ?", (kind, task_id))
        cur = c.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'blocked', ?, ?)",
                        (task_id, json.dumps({"reason": reason}), now))
        return cur.lastrowid


def set_status(db, task_id, status, completed=False):
    now = int(time.time())
    with conn(db) as c:
        c.execute("UPDATE tasks SET status = ?, completed_at = CASE WHEN ? THEN ? ELSE completed_at END WHERE id = ?",
                  (status, completed, now, task_id))
        c.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, NULL, ?)",
                  (task_id, status, now))


def add_run(db, task_id, outcome="completed", metadata=None, ended_ago=600, status="done"):
    now = int(time.time())
    with conn(db) as c:
        c.execute("INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome, metadata) VALUES (?,?,?,?,?,?)",
                  (task_id, status, now - ended_ago - 300, now - ended_ago, outcome, json.dumps(metadata or {})))


def comment(db, task_id, author, body):
    with conn(db) as c:
        c.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?,?,?,?)",
                  (task_id, author, body, int(time.time())))
