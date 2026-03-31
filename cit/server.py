"""cit — Context Information Tracker. MCP server for Claude Code."""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .store import Branch, Store

# Initialize
DB_PATH = Path(os.environ.get("CIT_DB", Path(__file__).parent.parent / "data" / "cit.db"))
store = Store(DB_PATH)
mcp = FastMCP("cit")


# ─── tmux helpers ────────────────────────────────────────────


def _in_tmux() -> bool:
    return bool(os.environ.get("TMUX"))


def _tmux_run(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["tmux", *args], capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _tmux_pane_alive(pane_id: str) -> bool:
    out = _tmux_run("list-panes", "-a", "-F", "#{pane_id}")
    return pane_id in out.splitlines() if out else False


def _tmux_split_and_run(command: str, target_pane: str | None = None,
                        vertical: bool = False) -> str | None:
    args = ["split-window", "-v" if vertical else "-h"]
    if target_pane:
        args.extend(["-t", target_pane])
    args.extend(["-P", "-F", "#{pane_id}", command])
    return _tmux_run(*args)


def _tmux_set_pane_title(pane_id: str, title: str) -> None:
    _tmux_run("select-pane", "-t", pane_id, "-T", title)


def _tmux_select_pane(pane_id: str) -> bool:
    return _tmux_run("select-pane", "-t", pane_id) is not None


# ─── session helpers ─────────────────────────────────────────


def _get_current_session_id() -> str | None:
    # 1. Explicit env var (set for forked/resumed sessions)
    env_id = os.environ.get("CLAUDE_SESSION_ID")
    if env_id:
        return env_id
    # 2. Look up current tmux pane in DB (reliable for main sessions)
    if _in_tmux():
        pane_id = _tmux_run("display-message", "-p", "#{pane_id}")
        if pane_id:
            branch = store.get_branch_by_pane_id(pane_id)
            if branch:
                return branch.id
    # 3. Fallback: latest transcript file (least reliable)
    transcript = _find_latest_transcript()
    return transcript.stem if transcript else None


def _find_latest_transcript() -> Path | None:
    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        return None
    latest: Path | None = None
    latest_mtime = 0.0
    for project_dir in claude_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for f in project_dir.glob("*.jsonl"):
            mtime = f.stat().st_mtime
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest = f
    return latest


def _get_transcript_path(session_id: str | None = None) -> Path | None:
    if not session_id:
        return _find_latest_transcript()
    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        return None
    for project_dir in claude_dir.iterdir():
        if not project_dir.is_dir():
            continue
        transcript = project_dir / f"{session_id}.jsonl"
        if transcript.exists():
            return transcript
    return None


def _count_transcript_messages(transcript_path: Path) -> int:
    count = 0
    try:
        for line in transcript_path.read_text().splitlines():
            if line.strip():
                count += 1
    except Exception:
        pass
    return count


def _auto_summarize_transcript(session_id: str, max_chars: int = 500) -> str:
    """Extract a summary from the transcript by collecting assistant message snippets."""
    transcript = _get_transcript_path(session_id)
    if not transcript or not transcript.exists():
        return ""
    assistant_texts: list[str] = []
    try:
        for line in transcript.read_text().splitlines():
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "assistant":
                content = msg.get("message", {}).get("content", [])
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block["text"].strip()
                        if text and len(text) > 20:
                            assistant_texts.append(text)
    except Exception:
        return ""
    if not assistant_texts:
        return ""
    combined = " | ".join(assistant_texts[-5:])
    if len(combined) > max_chars:
        combined = combined[:max_chars] + "..."
    return combined


def _pending_age_str(created_at: str) -> str:
    try:
        created = datetime.fromisoformat(created_at)
        delta = datetime.now() - created
        hours = delta.total_seconds() / 3600
        if hours < 1:
            return f"{int(delta.total_seconds() / 60)}m ago"
        if hours < 24:
            return f"{int(hours)}h ago"
        return f"{int(hours / 24)}d ago"
    except Exception:
        return ""


# ─── rendering ───────────────────────────────────────────────


def _render_tree() -> str:
    branches = store.get_all_branches()
    if not branches:
        return "(empty tree — use `cit branch <label>` to create your first branch)"

    children_map: dict[str | None, list[Branch]] = {}
    for b in branches:
        children_map.setdefault(b.parent_id, []).append(b)

    current_session = _get_current_session_id()
    lines: list[str] = []

    def render(branch: Branch, prefix: str, is_last: bool):
        connector = "└─" if is_last else "├─"
        is_current = branch.id == current_session
        marker = " ◀ current" if is_current else ""

        has_summary = branch.summary is not None
        pane_alive = _tmux_pane_alive(branch.tmux_pane_id) if branch.tmux_pane_id else False
        if pane_alive and has_summary:
            status_icon = "◆"
        elif pane_alive:
            status_icon = "●"
        elif has_summary:
            status_icon = "◈"
        else:
            status_icon = "○"

        label = branch.label or branch.id[:8]
        session_short = branch.id[:12] if branch.id else "?"

        pending_info = ""
        if branch.id.startswith("pending-"):
            age = _pending_age_str(branch.created_at)
            pending_info = f" [pending{', ' + age if age else ''}]"

        lines.append(f"{prefix}{connector}{status_icon} {label} ({session_short}){pending_info}{marker}")

        kids = children_map.get(branch.id, [])
        child_prefix = prefix + ("  " if is_last else "│ ")
        for i, kid in enumerate(kids):
            render(kid, child_prefix, i == len(kids) - 1)

    roots = children_map.get(None, [])
    for i, root in enumerate(roots):
        render(root, "", i == len(roots) - 1)

    total = len(branches)
    squashed = sum(1 for b in branches if b.summary is not None)
    pending = sum(1 for b in branches if b.id.startswith("pending-"))
    lines.append("")
    footer = f"Branches: {total} | Squashed: {squashed}"
    if pending:
        footer += f" | Pending: {pending}"
    lines.append(footer)

    return "\n".join(lines)


# ─── subcommand implementations ──────────────────────────────


def _cmd_branch(arg: str) -> str:
    """Create a new branch from the current session."""
    label = arg.strip()
    if not label:
        return "❌ Usage: cit branch <label>"

    try:
        parent_id = _get_current_session_id()
        if not parent_id:
            return "❌ Cannot detect current session ID."

        if not store.get_branch(parent_id):
            store.add_branch(Branch(id=parent_id, label="main"))

        if _in_tmux():
            current_pane = _tmux_run("display-message", "-p", "#{pane_id}")
            if current_pane:
                store.update_branch(parent_id, tmux_pane_id=current_pane)

        transcript = _get_transcript_path(parent_id)
        msg_index = _count_transcript_messages(transcript) if transcript else 0

        placeholder_id = f"pending-{uuid.uuid4().hex[:8]}"
        store.add_branch(Branch(
            id=placeholder_id, parent_id=parent_id, label=label,
            fork_point_msg_index=msg_index,
        ))
        store.record_metric(parent_id, "fork", {"label": label, "msg_index": msg_index})

        fork_cmd = f"claude --fork-session {parent_id}"

        if _in_tmux():
            tmux_cmd = f"CIT_PENDING_ID={placeholder_id} {fork_cmd}"
            parent_branch = store.get_branch(parent_id)
            siblings = store.get_children(parent_id)
            live_sibling_pane = None
            for sib in siblings:
                if sib.id != placeholder_id and sib.tmux_pane_id and _tmux_pane_alive(sib.tmux_pane_id):
                    live_sibling_pane = sib.tmux_pane_id

            if live_sibling_pane:
                pane_id = _tmux_split_and_run(tmux_cmd, target_pane=live_sibling_pane, vertical=True)
            else:
                target = parent_branch.tmux_pane_id if parent_branch and parent_branch.tmux_pane_id else None
                pane_id = _tmux_split_and_run(tmux_cmd, target_pane=target)
            if pane_id:
                store.update_branch(placeholder_id, tmux_pane_id=pane_id)
                _tmux_set_pane_title(pane_id, label)
                return (
                    f"🌿 Branch '{label}' — new pane opened!\n"
                    f"   Pane: {pane_id} | Fork point: msg #{msg_index}"
                )

        return (
            f"🌿 Branch '{label}' recorded.\n"
            f"   Fork point: msg #{msg_index}\n\n"
            f"Run manually:\n   {fork_cmd}\n"
            f"Then: cit init <session_id> {placeholder_id}"
        )
    except Exception as e:
        return f"❌ Error: {e}"


def _cmd_switch(arg: str) -> str:
    """Switch to a different branch."""
    branch = arg.strip()
    if not branch:
        return "❌ Usage: cit switch <branch>"

    try:
        current = _get_current_session_id()
        branch_id = store.find_branch_by_prefix(branch, current_session_id=current)
        if not branch_id:
            return f"❌ Branch not found: '{branch}'\n\nUse `cit log` to see branches."

        b = store.get_branch(branch_id)
        if not b:
            return f"❌ Branch data not found for '{branch_id}'"

        if current:
            store.record_metric(current, "checkout_from", {"to": branch_id})
        store.record_metric(branch_id, "checkout_to", {"from": current or "unknown"})

        children = store.get_children(branch_id)
        new_squashes = sum(1 for c in children if c.summary is not None)
        inbox_hint = f"\n📬 {new_squashes} squash(es) — run `cit inbox` to view" if new_squashes else ""

        resume_cmd = f"CLAUDE_SESSION_ID={b.id} claude --resume {b.id}"

        if _in_tmux():
            if b.tmux_pane_id and _tmux_pane_alive(b.tmux_pane_id):
                _tmux_select_pane(b.tmux_pane_id)
                return f"🔀 Switched to '{b.label}' ({b.id[:12]}){inbox_hint}"
            else:
                live_sibling_pane = None
                if b.parent_id:
                    siblings = store.get_children(b.parent_id)
                    for sib in siblings:
                        if sib.id != branch_id and sib.tmux_pane_id and _tmux_pane_alive(sib.tmux_pane_id):
                            live_sibling_pane = sib.tmux_pane_id

                if live_sibling_pane:
                    pane_id = _tmux_split_and_run(resume_cmd, target_pane=live_sibling_pane, vertical=True)
                else:
                    parent = store.get_branch(b.parent_id) if b.parent_id else None
                    target = parent.tmux_pane_id if parent and parent.tmux_pane_id and _tmux_pane_alive(parent.tmux_pane_id) else None
                    pane_id = _tmux_split_and_run(resume_cmd, target_pane=target)
                if pane_id:
                    store.update_branch(branch_id, tmux_pane_id=pane_id)
                    _tmux_set_pane_title(pane_id, b.label)
                    return f"🔀 Resumed '{b.label}' ({b.id[:12]}){inbox_hint}"

        return (
            f"🔀 Switch to '{b.label}' ({b.id[:12]}){inbox_hint}\n\n"
            f"Run: {resume_cmd}"
        )
    except Exception as e:
        return f"❌ Error: {e}"


def _cmd_squash(arg: str) -> str:
    """Save summary and switch to parent."""
    summary = arg.strip()
    if not summary:
        return "❌ Usage: cit squash <summary>\n   You (the model) must auto-generate a 1-3 sentence summary of this conversation's key findings. Do NOT ask the user — just write it yourself."

    try:
        current = _get_current_session_id()
        if not current:
            return "❌ Cannot detect current session ID."

        b = store.get_branch(current)
        if not b:
            return "❌ Current branch not registered."

        if not b.parent_id:
            return "❌ Cannot squash a root branch."

        store.update_branch(current, summary=summary)
        store.record_metric(current, "squash", {"summary": summary})

        parent = store.get_branch(b.parent_id)
        switch_msg = ""
        if parent and _in_tmux():
            if parent.tmux_pane_id and _tmux_pane_alive(parent.tmux_pane_id):
                _tmux_select_pane(parent.tmux_pane_id)
                switch_msg = f"\n🔀 Switched to parent: '{parent.label}'"

        return (
            f"◆ Squashed '{b.label}' ({current[:12]})\n"
            f"   Summary: {summary}{switch_msg}"
        )
    except Exception as e:
        return f"❌ Error: {e}"


def _cmd_close(arg: str) -> str:
    """Close a branch's tmux pane (does not delete the branch)."""
    try:
        current = _get_current_session_id()

        if arg.strip():
            branch_id = store.find_branch_by_prefix(arg.strip(), current_session_id=current)
            if not branch_id:
                return f"❌ Branch not found: '{arg}'"
        else:
            branch_id = current
            if not branch_id:
                return "❌ Cannot detect current session ID."

        b = store.get_branch(branch_id)
        if not b:
            return f"❌ Branch not found."

        if not b.tmux_pane_id:
            return f"⚠️ '{b.label}' has no pane."

        if not _tmux_pane_alive(b.tmux_pane_id):
            return f"⚠️ '{b.label}' pane is already closed."

        # Don't allow closing your own pane without switching first
        if branch_id == current:
            # Switch to parent first if possible
            if b.parent_id:
                parent = store.get_branch(b.parent_id)
                if parent and parent.tmux_pane_id and _tmux_pane_alive(parent.tmux_pane_id):
                    _tmux_select_pane(parent.tmux_pane_id)

        subprocess.run(["tmux", "kill-pane", "-t", b.tmux_pane_id],
                        capture_output=True, check=False)
        return f"✅ Closed pane for '{b.label}'. Branch data preserved — use `cit switch {b.label}` to reopen."

    except Exception as e:
        return f"❌ Error: {e}"


def _cmd_log(arg: str) -> str:
    """Show the branch tree."""
    try:
        return _render_tree()
    except Exception as e:
        return f"❌ Error: {e}"


def _cmd_inbox(arg: str) -> str:
    """Show child branch summaries."""
    try:
        current = _get_current_session_id()

        if arg.strip():
            branch_id = store.find_branch_by_prefix(arg.strip(), current_session_id=current)
            if not branch_id:
                return f"❌ Branch not found: '{arg}'"
        else:
            branch_id = current
            if not branch_id:
                return "❌ Cannot detect current session ID."

        b = store.get_branch(branch_id)
        if not b:
            return f"❌ Branch not found for '{branch_id}'"

        children = store.get_children(branch_id)
        with_summary = [c for c in children if c.summary]

        if not with_summary:
            n = len(children)
            if n == 0:
                return f"📭 No child branches for '{b.label}'."
            return f"📭 {n} child branch(es), none squashed yet."

        lines = [f"📬 Squash results for '{b.label}':"]
        for c in with_summary:
            pane_alive = _tmux_pane_alive(c.tmux_pane_id) if c.tmux_pane_id else False
            state = "live" if pane_alive else "suspended"
            lines.append(f"  - {c.label} ({state}): {c.summary}")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Error: {e}"


def _cmd_status(arg: str) -> str:
    """Show statistics."""
    try:
        branches = store.get_all_branches()
        if not branches:
            return "No branches tracked yet."

        total = len(branches)
        squashed = sum(1 for b in branches if b.summary is not None)
        pending = sum(1 for b in branches if b.id.startswith("pending-"))

        parent_map = {b.id: b.parent_id for b in branches}
        def depth(bid):
            d = 0
            while bid and bid in parent_map:
                bid = parent_map[bid]
                d += 1
            return d
        max_depth = max(depth(b.id) for b in branches) if branches else 0

        metrics = store.get_metrics()
        forks = sum(1 for m in metrics if m.get("event_type") == "fork")
        checkouts = sum(1 for m in metrics if m.get("event_type") and "checkout" in m["event_type"])
        squash_events = sum(1 for m in metrics if m.get("event_type") == "squash")

        return (
            f"=== cit status ===\n"
            f"Branches:  {total} (squashed: {squashed}, pending: {pending})\n"
            f"Depth:     {max_depth}\n"
            f"Forks:     {forks}\n"
            f"Switches:  {checkouts}\n"
            f"Squashes:  {squash_events}\n"
            f"Events:    {len(metrics)}"
        )
    except Exception as e:
        return f"❌ Error: {e}"


def _cmd_init(arg: str) -> str:
    """Register a session (usually auto, but manual fallback)."""
    try:
        parts = arg.strip().split()
        actual_session_id = parts[0] if len(parts) >= 1 else ""
        placeholder_id = parts[1] if len(parts) >= 2 else ""
        label = parts[2] if len(parts) >= 3 else ""

        session_id = actual_session_id or _get_current_session_id()
        if not session_id:
            return "❌ Cannot detect session ID."

        if placeholder_id:
            old = store.get_branch(placeholder_id)
            if old:
                if session_id == old.parent_id:
                    return f"❌ Session ID matches parent. Provide the real session ID."
                pane_id = old.tmux_pane_id
                if not pane_id and _in_tmux():
                    pane_id = _tmux_run("display-message", "-p", "#{pane_id}")
                store.add_branch(Branch(
                    id=session_id, parent_id=old.parent_id, label=old.label,
                    fork_point_msg_index=old.fork_point_msg_index, tmux_pane_id=pane_id,
                ))
                store.conn.execute("DELETE FROM branches WHERE id = ?", (placeholder_id,))
                store.conn.commit()
                return f"✅ Registered {session_id[:12]} as '{old.label}'"
            return f"❌ Placeholder '{placeholder_id}' not found"

        if store.get_branch(session_id):
            b = store.get_branch(session_id)
            return f"ℹ️ Already registered as '{b.label}'"

        store.add_branch(Branch(id=session_id, label=label or "main"))
        return f"✅ Registered {session_id[:12]} as root '{label or 'main'}'"
    except Exception as e:
        return f"❌ Error: {e}"


def _cmd_cleanup(arg: str) -> str:
    """Clean up stale placeholders."""
    try:
        hours = int(arg.strip()) if arg.strip() else 24
        deleted = store.delete_stale_placeholders(hours)
        if deleted == 0:
            return f"✅ No stale placeholders (threshold: {hours}h)"
        return f"🧹 Cleaned up {deleted} placeholder(s) older than {hours}h"
    except ValueError:
        return "❌ Usage: cit cleanup [hours]"
    except Exception as e:
        return f"❌ Error: {e}"


# ─── command dispatch ────────────────────────────────────────

_COMMANDS = {
    "branch": _cmd_branch,
    "switch": _cmd_switch,
    "squash": _cmd_squash,
    "close": _cmd_close,
    "log": _cmd_log,
    "inbox": _cmd_inbox,
    "status": _cmd_status,
    "init": _cmd_init,
    "cleanup": _cmd_cleanup,
}

_HELP = """cit — Context Information Tracker

Commands:
  cit branch <label>      Create a new branch from current session
  cit switch <branch>     Switch to a different branch
  cit squash <summary>    Save summary and switch to parent
  cit close [branch]      Close a branch's pane (keeps data)
  cit log                 Show branch tree
  cit inbox [branch]      Show child branch summaries
  cit status              Show statistics
  cit init [session] [placeholder]   Register session (usually automatic)
  cit cleanup [hours]     Clean up stale placeholders

Icons: ● exploring  ◆ squashed+live  ◈ squashed+suspended  ○ suspended"""


# ─── MCP tool ────────────────────────────────────────────────


@mcp.tool()
def cit(command: str, arg: str = "") -> str:
    """Context Information Tracker — git-style branching for Claude Code.

    Usage: cit <command> [arg]

    Commands:
      branch <label>      — Create a new branch (fork a sub-conversation)
      switch <branch>     — Switch to branch (resume if suspended)
      squash <summary>    — Summarize this branch for parent, then switch back
      close [branch]      — Close a branch's pane (data preserved)
      log                 — Show branch tree
      inbox [branch]      — Show child summaries
      status              — Statistics
      init                — Register session (usually automatic)
      cleanup [hours]     — Clean stale placeholders

    IMPORTANT for squash: When the user asks to squash (e.g. "cit squash"),
    YOU (the model) MUST auto-generate a 1-3 sentence summary of key conclusions,
    decisions, and findings from this conversation. Do NOT ask the user to provide
    the summary — write it yourself based on the conversation context, then call
    cit(command="squash", arg="<your generated summary>").

    Args:
        command: Subcommand (branch, switch, squash, log, inbox, status, init, cleanup)
        arg: Argument for the subcommand (label, branch name, summary, etc.)
    """
    cmd = command.strip().lower()
    handler = _COMMANDS.get(cmd)
    if not handler:
        return _HELP
    return handler(arg)


# ─── MCP Resources ──────────────────────────────────────────


@mcp.resource("cit://log")
def resource_log() -> str:
    """Current branch tree."""
    return _render_tree()


@mcp.resource("cit://status")
def resource_status() -> str:
    """Tree statistics."""
    return _cmd_status("")


# ─── Entry point ─────────────────────────────────────────────


def main():
    mcp.run()


if __name__ == "__main__":
    main()
