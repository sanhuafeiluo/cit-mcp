"""cit — Context Information Tracker. MCP server."""

from __future__ import annotations

import json
import os
import re
import shlex
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

RUNTIME_CLAUDE = "claude"
RUNTIME_CODEX = "codex"
_VALID_RUNTIMES = {RUNTIME_CLAUDE, RUNTIME_CODEX}


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


def _tmux_pane_file_path() -> Path:
    override = os.environ.get("CIT_TMUX_PANE_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "state" / "cit" / "tmux_pane_id"


def _read_tmux_pane_file() -> str | None:
    pane_file = _tmux_pane_file_path()
    try:
        text = pane_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if text and _tmux_pane_alive(text):
        return text
    return None


def _tmux_current_pane_id_from_clients() -> str | None:
    clients = _tmux_run("list-clients", "-F", "#{client_tty}\t#{client_activity}")
    if not clients:
        return None

    ranked: list[tuple[int, str]] = []
    for line in clients.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        tty, activity = parts
        if not tty:
            continue
        try:
            ts = int(activity)
        except ValueError:
            ts = 0
        ranked.append((ts, tty))

    ranked.sort(reverse=True)
    for _, tty in ranked:
        pane_id = _tmux_run("display-message", "-p", "-c", tty, "#{pane_id}")
        if pane_id:
            return pane_id
    return None


def _tmux_current_pane_id() -> str | None:
    if _in_tmux():
        pane_id = _tmux_run("display-message", "-p", "#{pane_id}")
        if pane_id:
            return pane_id

    explicit = os.environ.get("CIT_TMUX_PANE_ID", "").strip()
    if explicit and _tmux_pane_alive(explicit):
        return explicit

    file_pane_id = _read_tmux_pane_file()
    if file_pane_id:
        return file_pane_id

    return _tmux_current_pane_id_from_clients()


def _tmux_available() -> bool:
    return _tmux_current_pane_id() is not None


# ─── session helpers ─────────────────────────────────────────


def _detect_runtime() -> str:
    forced = os.environ.get("CIT_RUNTIME", "").strip().lower()
    if forced in _VALID_RUNTIMES:
        return forced
    if os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_MANAGED_BY_NPM"):
        return RUNTIME_CODEX
    return RUNTIME_CLAUDE


def _session_env_var(runtime: str) -> str:
    return "CODEX_THREAD_ID" if runtime == RUNTIME_CODEX else "CLAUDE_SESSION_ID"


def _runtime_cli(runtime: str) -> str:
    return "codex" if runtime == RUNTIME_CODEX else "claude"


def _extract_codex_session_id(path: Path) -> str | None:
    # rollout-...-<uuid>.jsonl
    matches = re.findall(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        path.stem,
        flags=re.IGNORECASE,
    )
    return matches[-1] if matches else None


def _transcript_session_id(path: Path, runtime: str) -> str | None:
    if runtime == RUNTIME_CODEX:
        return _extract_codex_session_id(path)
    return path.stem


def _iter_transcripts(runtime: str):
    if runtime == RUNTIME_CODEX:
        sessions_dir = Path.home() / ".codex" / "sessions"
        if not sessions_dir.exists():
            return
        yield from sessions_dir.rglob("*.jsonl")
        return

    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        return
    for project_dir in claude_dir.iterdir():
        if not project_dir.is_dir():
            continue
        yield from project_dir.glob("*.jsonl")


def _get_current_session_id(runtime: str | None = None) -> str | None:
    runtime = runtime or _detect_runtime()

    # 1. Explicit env var (set for forked/resumed sessions)
    env_id = os.environ.get(_session_env_var(runtime))
    if env_id:
        return env_id
    # 2. Look up current tmux pane in DB (reliable for pane-managed sessions)
    pane_id = _tmux_current_pane_id()
    if pane_id:
        branch = store.get_branch_by_pane_id(pane_id, runtime=runtime)
        if branch:
            return branch.id
    # 3. Fallback: latest transcript file (least reliable)
    transcript = _find_latest_transcript(runtime)
    return _transcript_session_id(transcript, runtime) if transcript else None


def _find_latest_transcript(runtime: str | None = None) -> Path | None:
    runtime = runtime or _detect_runtime()
    latest: Path | None = None
    latest_mtime = 0.0
    for transcript in _iter_transcripts(runtime) or ():
        try:
            mtime = transcript.stat().st_mtime
        except OSError:
            continue
        if mtime > latest_mtime:
            latest_mtime = mtime
            latest = transcript
    return latest


def _get_transcript_path(session_id: str | None = None, runtime: str | None = None) -> Path | None:
    runtime = runtime or _detect_runtime()

    if not session_id:
        return _find_latest_transcript(runtime)

    if runtime == RUNTIME_CODEX:
        latest: Path | None = None
        latest_mtime = 0.0
        for transcript in _iter_transcripts(runtime) or ():
            if session_id not in transcript.name:
                continue
            try:
                mtime = transcript.stat().st_mtime
            except OSError:
                continue
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest = transcript
        return latest

    claude_dir = Path.home() / ".claude" / "projects"
    if claude_dir.exists():
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


def _auto_summarize_transcript(
    session_id: str, runtime: str | None = None, max_chars: int = 500,
) -> str:
    """Extract a summary from the transcript by collecting assistant message snippets."""
    transcript = _get_transcript_path(session_id, runtime=runtime)
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


def _pending_init_cmd(pending_id: str) -> str:
    return f"cit init {pending_id}"


def _bootstrap_prompt(runtime: str, branch_label: str, pending_id: str | None = None) -> str:
    if runtime == RUNTIME_CODEX:
        init_hint = "command='init'"
        if pending_id:
            init_hint = f"command='init', arg='{pending_id}'"
        return (
            f"You are now in cit branch '{branch_label}'. First, call the cit MCP tool with "
            f"{init_hint}. Then call cit(command='inbox') to pick up child summaries before continuing."
        )
    return ""


def _fork_command(
    runtime: str, parent_id: str, branch_label: str, pending_id: str | None = None,
) -> str:
    cli = _runtime_cli(runtime)
    if runtime == RUNTIME_CODEX:
        prompt = _bootstrap_prompt(runtime, branch_label, pending_id=pending_id)
        return f"{cli} fork {parent_id} {shlex.quote(prompt)}"
    return f"{cli} --fork-session {parent_id}"


def _resume_command(
    runtime: str, branch_id: str, branch_label: str, pending_id: str | None = None,
) -> str:
    cli = _runtime_cli(runtime)
    if runtime == RUNTIME_CODEX:
        prompt = _bootstrap_prompt(runtime, branch_label, pending_id=pending_id)
        return f"{cli} resume {branch_id} {shlex.quote(prompt)}"
    return f"CLAUDE_SESSION_ID={branch_id} {cli} --resume {branch_id}"


def _auto_register_pending_session(session_id: str, runtime: str) -> None:
    pending_id = os.environ.get("CIT_PENDING_ID")
    if not pending_id:
        return

    old = store.get_branch(pending_id, runtime=runtime)
    if not old or session_id == old.parent_id:
        return

    if store.get_branch(session_id, runtime=runtime):
        store.conn.execute(
            "DELETE FROM branches WHERE id = ? AND runtime = ?",
            (pending_id, runtime),
        )
        store.conn.commit()
        return

    pane_id = old.tmux_pane_id
    if not pane_id:
        pane_id = _tmux_current_pane_id()

    store.add_branch(Branch(
        id=session_id,
        parent_id=old.parent_id,
        label=old.label,
        fork_point_msg_index=old.fork_point_msg_index,
        tmux_pane_id=pane_id,
        runtime=runtime,
    ))
    store.conn.execute(
        "DELETE FROM branches WHERE id = ? AND runtime = ?",
        (pending_id, runtime),
    )
    store.conn.commit()


def _ensure_session_registered(session_id: str, runtime: str) -> None:
    if store.get_branch(session_id, runtime=runtime):
        return

    pane_id = _tmux_current_pane_id()

    # Codex has no hook-equivalent safety net like Claude; keep registration
    # conservative and never rewrite existing parent/child edges.
    if runtime == RUNTIME_CODEX:
        store.add_branch(Branch(
            id=session_id,
            label="main",
            tmux_pane_id=pane_id,
            runtime=runtime,
        ))
        return

    main_roots = [r for r in store.get_roots(runtime=runtime) if r.label == "main"]
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

    store.add_branch(Branch(
        id=session_id,
        label="main",
        tmux_pane_id=pane_id,
        summary=old_summary,
        runtime=runtime,
    ))


def _bootstrap_runtime_session(ensure_root: bool = True) -> tuple[str | None, str]:
    runtime = _detect_runtime()
    session_id = _get_current_session_id(runtime=runtime)
    if not session_id:
        return None, runtime

    _auto_register_pending_session(session_id, runtime)
    if ensure_root:
        _ensure_session_registered(session_id, runtime)

    pane_id = _tmux_current_pane_id()
    if pane_id:
        branch = store.get_branch(session_id, runtime=runtime)
        if branch and branch.tmux_pane_id != pane_id:
            store.update_branch(session_id, runtime=runtime, tmux_pane_id=pane_id)

    return session_id, runtime


# ─── rendering ───────────────────────────────────────────────


def _render_tree(runtime: str | None = None) -> str:
    runtime = runtime or _detect_runtime()
    branches = store.get_all_branches(runtime=runtime)
    if not branches:
        return "(empty tree — use `cit branch <label>` to create your first branch)"

    children_map: dict[str | None, list[Branch]] = {}
    for b in branches:
        children_map.setdefault(b.parent_id, []).append(b)

    current_session = _get_current_session_id(runtime=runtime)
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
    footer = f"Runtime: {runtime} | Branches: {total} | Squashed: {squashed}"
    if pending:
        footer += f" | Pending: {pending}"
    lines.append(footer)
    if pending:
        lines.append("Pending recovery:")
        pending_branches = [b for b in branches if b.id.startswith("pending-")]
        for b in pending_branches[:5]:
            label = b.label or b.id[:8]
            lines.append(
                f"  - {label}: start/switch this branch, then run `{_pending_init_cmd(b.id)}` in the child session."
            )
        if len(pending_branches) > 5:
            lines.append(f"  - ... and {len(pending_branches) - 5} more pending branch(es)")

    return "\n".join(lines)


# ─── subcommand implementations ──────────────────────────────


def _cmd_branch(arg: str) -> str:
    """Create a new branch from the current session."""
    label = arg.strip()
    if not label:
        return "❌ Usage: cit branch <label>"

    try:
        runtime = _detect_runtime()
        parent_id = _get_current_session_id(runtime=runtime)
        if not parent_id:
            return "❌ Cannot detect current session ID."

        if not store.get_branch(parent_id, runtime=runtime):
            store.add_branch(Branch(id=parent_id, label="main", runtime=runtime))

        tmux_ready = _tmux_available()

        if tmux_ready:
            current_pane = _tmux_current_pane_id()
            if current_pane:
                store.update_branch(parent_id, runtime=runtime, tmux_pane_id=current_pane)

        transcript = _get_transcript_path(parent_id, runtime=runtime)
        msg_index = _count_transcript_messages(transcript) if transcript else 0

        placeholder_id = f"pending-{uuid.uuid4().hex[:8]}"
        store.add_branch(Branch(
            id=placeholder_id, parent_id=parent_id, label=label,
            fork_point_msg_index=msg_index,
            runtime=runtime,
        ))
        store.record_metric(
            parent_id, "fork", {"label": label, "msg_index": msg_index}, runtime=runtime,
        )

        fork_cmd = _fork_command(
            runtime, parent_id, label,
            pending_id=placeholder_id if runtime == RUNTIME_CODEX else None,
        )

        if tmux_ready:
            tmux_cmd = f"CIT_PENDING_ID={placeholder_id} {fork_cmd}"
            parent_branch = store.get_branch(parent_id, runtime=runtime)
            siblings = store.get_children(parent_id, runtime=runtime)
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
                store.update_branch(placeholder_id, runtime=runtime, tmux_pane_id=pane_id)
                _tmux_set_pane_title(pane_id, label)
                return (
                    f"🌿 Branch '{label}' — new pane opened!\n"
                    f"   Pane: {pane_id} | Fork point: msg #{msg_index}\n"
                    f"   If this branch remains pending, run `{_pending_init_cmd(placeholder_id)}` in the child session."
                )

        mode_note = (
            "⚠️ Auto mode unavailable (tmux context not detected or pane split failed).\n"
            if runtime == RUNTIME_CODEX else
            ""
        )
        return (
            f"{mode_note}"
            f"🌿 Branch '{label}' recorded.\n"
            f"   Fork point: msg #{msg_index}\n\n"
            f"Run manually:\n   {fork_cmd}\n"
            f"Then in the child session run:\n   {_pending_init_cmd(placeholder_id)}"
        )
    except Exception as e:
        return f"❌ Error: {e}"


def _cmd_switch(arg: str) -> str:
    """Switch to a different branch."""
    branch = arg.strip()
    if not branch:
        return "❌ Usage: cit switch <branch>"

    try:
        runtime = _detect_runtime()
        current = _get_current_session_id(runtime=runtime)
        branch_id = store.find_branch_by_prefix(
            branch, current_session_id=current, runtime=runtime,
        )
        if not branch_id:
            return f"❌ Branch not found: '{branch}'\n\nUse `cit log` to see branches."

        b = store.get_branch(branch_id, runtime=runtime)
        if not b:
            return f"❌ Branch data not found for '{branch_id}'"

        if current:
            store.record_metric(
                current, "checkout_from", {"to": branch_id}, runtime=runtime,
            )
        store.record_metric(
            branch_id, "checkout_to", {"from": current or "unknown"}, runtime=runtime,
        )

        children = store.get_children(branch_id, runtime=runtime)
        new_squashes = sum(1 for c in children if c.summary is not None)
        inbox_hint = f"\n📬 {new_squashes} squash(es) — run `cit inbox` to view" if new_squashes else ""
        pending_hint = ""
        is_pending_placeholder = b.id.startswith("pending-")
        if is_pending_placeholder:
            pending_hint = (
                f"\n🛠 Pending branch bootstrap: run `{_pending_init_cmd(b.id)}` "
                "in the child session after it starts."
            )

        if is_pending_placeholder:
            if not b.parent_id:
                return f"❌ Pending branch '{b.label}' is missing parent session."
            launch_cmd = _fork_command(
                runtime, b.parent_id, b.label or b.id[:8],
                pending_id=b.id if runtime == RUNTIME_CODEX else None,
            )
        else:
            launch_cmd = _resume_command(runtime, b.id, b.label or b.id[:8])

        tmux_ready = _tmux_available()
        if tmux_ready:
            if b.tmux_pane_id and _tmux_pane_alive(b.tmux_pane_id):
                _tmux_select_pane(b.tmux_pane_id)
                return f"🔀 Switched to '{b.label}' ({b.id[:12]}){inbox_hint}{pending_hint}"
            else:
                live_sibling_pane = None
                if b.parent_id:
                    siblings = store.get_children(b.parent_id, runtime=runtime)
                    for sib in siblings:
                        if sib.id != branch_id and sib.tmux_pane_id and _tmux_pane_alive(sib.tmux_pane_id):
                            live_sibling_pane = sib.tmux_pane_id

                if live_sibling_pane:
                    pane_id = _tmux_split_and_run(launch_cmd, target_pane=live_sibling_pane, vertical=True)
                else:
                    parent = store.get_branch(b.parent_id, runtime=runtime) if b.parent_id else None
                    target = parent.tmux_pane_id if parent and parent.tmux_pane_id and _tmux_pane_alive(parent.tmux_pane_id) else None
                    pane_id = _tmux_split_and_run(launch_cmd, target_pane=target)
                if pane_id:
                    store.update_branch(branch_id, runtime=runtime, tmux_pane_id=pane_id)
                    _tmux_set_pane_title(pane_id, b.label)
                    if is_pending_placeholder:
                        return f"🌿 Started pending branch '{b.label}' ({b.id[:12]}){inbox_hint}{pending_hint}"
                    return f"🔀 Resumed '{b.label}' ({b.id[:12]}){inbox_hint}{pending_hint}"

        mode_note = (
            "⚠️ Auto mode unavailable (tmux context not detected or pane split failed).\n"
            if runtime == RUNTIME_CODEX else
            ""
        )
        return (
            f"{mode_note}"
            f"🔀 Switch to '{b.label}' ({b.id[:12]}){inbox_hint}\n\n"
            f"Run: {launch_cmd}{pending_hint}"
        )
    except Exception as e:
        return f"❌ Error: {e}"


def _cmd_squash(arg: str) -> str:
    """Save summary and switch to parent."""
    summary = arg.strip()
    if not summary:
        return "❌ Usage: cit squash <summary>\n   You (the model) must auto-generate a 1-3 sentence summary of this conversation's key findings. Do NOT ask the user — just write it yourself."

    try:
        runtime = _detect_runtime()
        current = _get_current_session_id(runtime=runtime)
        if not current:
            return "❌ Cannot detect current session ID."

        b = store.get_branch(current, runtime=runtime)
        if not b:
            return "❌ Current branch not registered."

        if not b.parent_id:
            return "❌ Cannot squash a root branch."

        store.update_branch(current, runtime=runtime, summary=summary)
        store.record_metric(current, "squash", {"summary": summary}, runtime=runtime)

        parent = store.get_branch(b.parent_id, runtime=runtime)
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
        runtime = _detect_runtime()
        current = _get_current_session_id(runtime=runtime)

        if arg.strip():
            branch_id = store.find_branch_by_prefix(
                arg.strip(), current_session_id=current, runtime=runtime,
            )
            if not branch_id:
                return f"❌ Branch not found: '{arg}'"
        else:
            branch_id = current
            if not branch_id:
                return "❌ Cannot detect current session ID."

        b = store.get_branch(branch_id, runtime=runtime)
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
                parent = store.get_branch(b.parent_id, runtime=runtime)
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
        runtime = _detect_runtime()
        current = _get_current_session_id(runtime=runtime)

        if arg.strip():
            branch_id = store.find_branch_by_prefix(
                arg.strip(), current_session_id=current, runtime=runtime,
            )
            if not branch_id:
                return f"❌ Branch not found: '{arg}'"
        else:
            branch_id = current
            if not branch_id:
                return "❌ Cannot detect current session ID."

        b = store.get_branch(branch_id, runtime=runtime)
        if not b:
            return f"❌ Branch not found for '{branch_id}'"

        children = store.get_children(branch_id, runtime=runtime)
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
        runtime = _detect_runtime()
        branches = store.get_all_branches(runtime=runtime)
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

        metrics = store.get_metrics(runtime=runtime)
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
        runtime = _detect_runtime()
        parts = arg.strip().split()
        actual_session_id = ""
        placeholder_id = ""
        label = ""

        if len(parts) == 1 and parts[0].startswith("pending-"):
            # Shorthand for Codex-style explicit handshake:
            # cit init <pending-id> -> bind current session to this placeholder.
            placeholder_id = parts[0]
        elif len(parts) >= 2:
            # Backward-compatible explicit form:
            # cit init <session_id> <pending_id> [label]
            actual_session_id = parts[0]
            placeholder_id = parts[1]
            label = parts[2] if len(parts) >= 3 else ""
        elif len(parts) == 1:
            # Existing root-register form:
            # cit init <session_id>
            actual_session_id = parts[0]

        session_id = actual_session_id or _get_current_session_id(runtime=runtime)
        if not session_id:
            return "❌ Cannot detect session ID."

        if placeholder_id:
            old = store.get_branch(placeholder_id, runtime=runtime)
            if old:
                if session_id == old.parent_id:
                    return f"❌ Session ID matches parent. Provide the real session ID."
                pane_id = old.tmux_pane_id
                if not pane_id:
                    pane_id = _tmux_current_pane_id()
                store.add_branch(Branch(
                    id=session_id, parent_id=old.parent_id, label=old.label,
                    fork_point_msg_index=old.fork_point_msg_index, tmux_pane_id=pane_id,
                    runtime=runtime,
                ))
                store.conn.execute(
                    "DELETE FROM branches WHERE id = ? AND runtime = ?",
                    (placeholder_id, runtime),
                )
                store.conn.commit()
                return f"✅ Registered {session_id[:12]} as '{old.label}'"
            return f"❌ Placeholder '{placeholder_id}' not found"

        if store.get_branch(session_id, runtime=runtime):
            b = store.get_branch(session_id, runtime=runtime)
            return f"ℹ️ Already registered as '{b.label}'"

        store.add_branch(Branch(id=session_id, label=label or "main", runtime=runtime))
        return f"✅ Registered {session_id[:12]} as root '{label or 'main'}'"
    except Exception as e:
        return f"❌ Error: {e}"


def _cmd_cleanup(arg: str) -> str:
    """Clean up stale placeholders."""
    try:
        hours = int(arg.strip()) if arg.strip() else 24
        runtime = _detect_runtime()
        deleted = store.delete_stale_placeholders(hours, runtime=runtime)
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
  cit init <pending-id>   Bind current session to a pending placeholder
  cit cleanup [hours]     Clean up stale placeholders

Icons: ● exploring  ◆ squashed+live  ◈ squashed+suspended  ○ suspended"""


# ─── MCP tool ────────────────────────────────────────────────


@mcp.tool()
def cit(command: str, arg: str = "") -> str:
    """Context Information Tracker — git-style branching for Claude Code and Codex.

    Usage: cit <command> [arg]

    Commands:
      branch <label>      — Create a new branch (fork a sub-conversation)
      switch <branch>     — Switch to branch (resume if suspended)
      squash <summary>    — Summarize this branch for parent, then switch back
      close [branch]      — Close a branch's pane (data preserved)
      log                 — Show branch tree
      inbox [branch]      — Show child summaries
      status              — Statistics
      init                — Register session (usually automatic; supports `init <pending-id>`)
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
    # Ensure session is registered on first tool call (for runtimes without hooks).
    # If the user is explicitly running `cit init <session> <placeholder>`, avoid
    # auto-creating a root before manual initialization.
    if cmd == "init" and arg.strip():
        _bootstrap_runtime_session(ensure_root=False)
    else:
        _bootstrap_runtime_session()
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
