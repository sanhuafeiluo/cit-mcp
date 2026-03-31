#!/usr/bin/env python3
"""SessionStart hook: auto-register sessions + inject branch context."""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from cit.store import Branch, Store

DB_PATH = Path(os.environ.get("CIT_DB", Path(__file__).parent.parent / "data" / "cit.db"))


def _detect_tmux_pane() -> str | None:
    if not os.environ.get("TMUX"):
        return None
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#{pane_id}"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


def _tmux_pane_alive(pane_id: str) -> bool:
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
            capture_output=True, text=True, timeout=5,
        )
        return pane_id in result.stdout.splitlines() if result.returncode == 0 else False
    except Exception:
        return False


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    session_id = hook_input.get("session_id") or os.environ.get("CLAUDE_SESSION_ID")
    if not session_id:
        return

    store = Store(DB_PATH)
    runtime = "claude"

    # ── Auto-register forked sessions ──
    pending_id = os.environ.get("CIT_PENDING_ID")
    if pending_id:
        old = store.get_branch(pending_id, runtime=runtime)
        if old and session_id != old.parent_id:
            store.add_branch(Branch(
                id=session_id, parent_id=old.parent_id, label=old.label,
                fork_point_msg_index=old.fork_point_msg_index,
                tmux_pane_id=old.tmux_pane_id,
                runtime=runtime,
            ))
            store.conn.execute(
                "DELETE FROM branches WHERE id = ? AND runtime = ?",
                (pending_id, runtime),
            )
            store.conn.commit()

    # ── Auto-register unknown sessions: reuse existing root main ──
    branch = store.get_branch(session_id, runtime=runtime)
    if not branch:
        pane_id = _detect_tmux_pane()

        # Find ALL root mains (may be multiple due to prior crashes/manual init)
        main_roots = [r for r in store.get_roots(runtime=runtime) if r.label == "main"]

        # Migrate children from every old root to new session, then delete them
        old_summary = None
        for old_root in main_roots:
            old_summary = old_root.summary or old_summary
            store.conn.execute(
                "UPDATE branches SET parent_id = ? WHERE parent_id = ? AND runtime = ?",
                (session_id, old_root.id, runtime),
            )
            store.conn.execute(
                "DELETE FROM branches WHERE id = ? AND runtime = ?",
                (old_root.id, runtime),
            )
        store.conn.commit()

        branch = Branch(
            id=session_id,
            label="main",
            tmux_pane_id=pane_id,
            summary=old_summary,
            runtime=runtime,
        )
        store.add_branch(branch)

    # ── Inject branch context ──
    path_labels = [branch.label or branch.id[:8]]
    current = branch
    visited = {current.id}
    while current.parent_id:
        if current.parent_id in visited:
            break
        visited.add(current.parent_id)
        parent = store.get_branch(current.parent_id, runtime=runtime)
        if not parent:
            break
        path_labels.append(parent.label or parent.id[:8])
        current = parent
    path_labels.reverse()

    breadcrumb = " > ".join(path_labels)
    context_msg = f"[cit] Branch: {breadcrumb}."
    context_msg += " Use the `cit` MCP tool for branch operations (e.g. cit squash, cit log). When the user says 'cit squash', auto-generate a 1-3 sentence summary and call cit(command='squash', arg='<your summary>')."
    if branch.summary:
        context_msg += f"\nSummary: {branch.summary}"

    children = store.get_children(session_id, runtime=runtime)
    squashed_children = [c for c in children if c.summary]
    if squashed_children:
        context_msg += "\nSquash results:"
        for c in squashed_children:
            context_msg += f"\n  - {c.label}: {c.summary}"

    store.close()
    print(context_msg)


if __name__ == "__main__":
    main()
