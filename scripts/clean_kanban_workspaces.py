#!/usr/bin/env python3
"""No-agent cron script (Hermes cron "Kanban workspace cleanup", deliver local): removes the work folders of
finished cards — scratch workspaces of every board and git worktrees under <repo>/.worktrees/<task_id>.

Rules (details and paths in werkmap_groei.py):
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
Skips the whole run while a drain restart runs (~/.hermes/state/drain.json). Lock shared with clean_previews.py.
``--dry-run`` prints every decision and planned action and changes nothing.
Real mode logs to ~/.hermes/logs/clean_kanban_workspaces.log; stdout gets one summary line only when something
was removed or failed (empty stdout = nothing to do).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import werkmap_groei as wg  # noqa: E402

if __name__ == "__main__":
    sys.exit(wg.run_cleanup("clean_kanban_workspaces", ("werkmap", "worktree"), sys.argv[1:]))
