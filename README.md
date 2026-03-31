# cit — Context Information Tracker

Git-style conversation branching for Claude Code and Codex CLI. [中文文档](docs/zh.md)

When you're deep in a conversation and hit an unfamiliar concept, open a branch to explore it. When you're done, squash it back — the parent branch gets a concise summary without the detour polluting its context.

```
main ●                         "Reading a PagedAttention paper"
└─ learn-mha ◈                 "How does Multi-Head Attention work?"
   └─ why-sqrt-dk ◈            "Why divide by √d_k?"
```

Each `squash` compresses an exploration into 1-3 sentences that bubble up to the parent. The parent sees only conclusions, not the journey.

## Setup

```bash
git clone https://github.com/v3nividiv1ci/cit-mcp.git && cd cit-mcp
./setup.sh
```

`setup.sh` handles everything:
- Creates Python venv and installs dependencies
- Registers the MCP server for Claude (`~/.claude.json`)
- Registers Claude session hooks (`~/.claude/settings.json`)
- Registers the MCP server for Codex (`codex mcp add`)

Targets:

```bash
./setup.sh --target claude   # Claude only
./setup.sh --target codex    # Codex only
./setup.sh --target both     # (default) install both
```

Restart Claude Code or Codex after setup.

**Requirements:** Python 3.11+, tmux (for multi-pane layout), Claude CLI and/or Codex CLI depending on target.

### Codex Auto-Mode Notes

Codex has no Claude-style session hooks. `cit` uses a bootstrap handshake for Codex branches:

1. `cit branch <label>` creates a `pending-*` placeholder.
2. Child session starts (`codex fork ...`) and calls `cit init <pending-id>`.
3. Placeholder is promoted to a real branch session.
4. The child Codex session must have its own session id; reusing the parent id makes `cit init` fail.

If tmux context is unavailable (or split fails), `cit` now prints a fallback command sequence.
In that fallback flow, run `cit init <pending-id>` in the child session to finalize registration.
For best results in Codex, launch it through `codex-tmux` so the current pane id is written to
`~/.local/state/cit/tmux_pane_id` before Codex starts.
Advanced setups can still override detection with `CIT_TMUX_PANE_ID=<pane-id>` or
`CIT_TMUX_PANE_FILE=<path>`.

## Commands

| Command | What it does |
|---------|-------------|
| `cit branch <label>` | Fork a sub-conversation (opens new tmux pane) |
| `cit switch <branch>` | Switch to a branch (reopens pane if closed) |
| `cit squash` | Summarize this branch, return to parent |
| `cit close [branch]` | Close a branch's pane (data preserved) |
| `cit log` | Show the branch tree |
| `cit inbox [branch]` | Read child branch summaries |
| `cit status` | Statistics |
| `cit cleanup [hours]` | Clean stale placeholders |
| `cit init <pending-id>` | Bind current child session to a pending branch |

Notes:
- Branch trees are isolated per runtime (Claude vs Codex). `cit log` shows the current runtime tree.

## Example: Recursive Learning

You're reading a paper and keep hitting unfamiliar concepts:

```
┌──────────┬──────────┬──────────┐
│          │          │          │
│  main    │ learn-mha│ why-sqrt │
│  (paper) │ (concept)│ (detail) │
│          │          │          │
└──────────┴──────────┴──────────┘
```

1. **main**: Reading a paper, hit "Multi-Head Attention"
2. `cit branch learn-mha` → new pane opens, explore MHA
3. Hit "√d_k scaling" → `cit branch why-sqrt-dk` → go deeper
4. Understand it → `cit squash` → auto-returns to learn-mha with summary
5. Finish MHA → `cit squash` → auto-returns to main with summary
6. `cit inbox` → main gets both summaries, continues the paper

**Why not sub-agents?** Sub-agents do "fetch me an answer." Branching is for **interactive exploration** — you ask, follow up, go deeper, at your own pace.

## Status Icons

| Icon | Meaning |
|------|---------|
| `●` | Exploring (pane alive, no summary) |
| `◆` | Squashed + pane alive |
| `◈` | Squashed + pane closed |
| `○` | Suspended (pane closed, no summary) |

## Layout

Children split right, siblings split down:

```
┌──────────┬──────────┐
│          │ child-A  │
│  main    ├──────────┤
│          │ child-B  │
└──────────┴──────────┘
```

## Uninstall

Claude: Remove the `cit` entry from `~/.claude.json` (`mcpServers.cit`) and the hook entries from `~/.claude/settings.json`.

Codex: `codex mcp remove cit`.
