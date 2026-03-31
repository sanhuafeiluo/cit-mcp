#!/usr/bin/env bash
set -euo pipefail

CIT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="both"

usage() {
    echo "Usage: ./setup.sh [--target claude|codex|both]"
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target)
            TARGET="${2:-}"
            shift 2
            ;;
        -h|--help)
            usage
            ;;
        *)
            usage
            ;;
    esac
done

TARGET="$(echo "$TARGET" | tr '[:upper:]' '[:lower:]')"
if [[ "$TARGET" != "claude" && "$TARGET" != "codex" && "$TARGET" != "both" ]]; then
    usage
fi

# ── Pre-flight checks ──
if ! command -v python3 &> /dev/null; then
    echo "❌ python3 not found"
    exit 1
fi

py_ver=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
if python3 -c 'import sys; exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    :
else
    echo "❌ Python 3.11+ required, found $py_ver"
    exit 1
fi

if [[ "$TARGET" == "claude" || "$TARGET" == "both" ]]; then
    if ! command -v claude &> /dev/null; then
        echo "⚠️  claude CLI not found — install Claude Code first"
        echo "   https://docs.anthropic.com/en/docs/claude-code"
        if [[ "$TARGET" == "claude" ]]; then
            exit 1
        fi
    fi
fi

if [[ "$TARGET" == "codex" || "$TARGET" == "both" ]]; then
    if ! command -v codex &> /dev/null; then
        echo "⚠️  codex CLI not found — install Codex CLI first"
        echo "   https://developers.openai.com/codex"
        if [[ "$TARGET" == "codex" ]]; then
            exit 1
        fi
    fi
fi

CLAUDE_JSON="$HOME/.claude.json"
CLAUDE_SETTINGS="$HOME/.claude/settings.json"

echo "=== cit setup (target: $TARGET) ==="
echo "Python: $py_ver | Install dir: $CIT_DIR"
echo ""

# ── 1. Python venv + dependencies ──
echo "Setting up Python venv..."
if [ ! -d "$CIT_DIR/.venv" ]; then
    python3 -m venv "$CIT_DIR/.venv"
fi
"$CIT_DIR/.venv/bin/pip" install -q -e "$CIT_DIR"
echo "  OK"

# ── 2. Register MCP server for Claude ──
if [[ "$TARGET" == "claude" || "$TARGET" == "both" ]]; then
echo "Registering Claude MCP server..."
PYTHON_PATH="$CIT_DIR/.venv/bin/python"

if [ ! -f "$CLAUDE_JSON" ]; then
    echo '{}' > "$CLAUDE_JSON"
fi

# Use python to safely merge JSON (handles missing keys, existing config)
python3 - "$CLAUDE_JSON" "$PYTHON_PATH" <<'PYEOF'
import json, sys

config_path, python_path = sys.argv[1], sys.argv[2]

with open(config_path) as f:
    config = json.load(f)

config.setdefault("mcpServers", {})
config["mcpServers"]["cit"] = {
    "type": "stdio",
    "command": python_path,
    "args": ["-m", "cit.server"],
    "env": {"CIT_RUNTIME": "claude"}
}

with open(config_path, "w") as f:
    json.dump(config, f, indent=2)

print("  OK — registered cit MCP server")
PYEOF
fi

# ── 3. Register hooks in ~/.claude/settings.json ──
if [[ "$TARGET" == "claude" || "$TARGET" == "both" ]]; then
echo "Registering Claude hooks..."

mkdir -p "$HOME/.claude"
if [ ! -f "$CLAUDE_SETTINGS" ]; then
    echo '{}' > "$CLAUDE_SETTINGS"
fi

python3 - "$CLAUDE_SETTINGS" "$CIT_DIR" <<'PYEOF'
import json, sys

settings_path, cit_dir = sys.argv[1], sys.argv[2]

with open(settings_path) as f:
    settings = json.load(f)

hooks_dir = f"{cit_dir}/hooks"

hook_defs = {
    "SessionStart": {
        "command": f"python3 {hooks_dir}/session_start.py",
        "timeout": 10,
    },
    "PreCompact": {
        "command": f"python3 {hooks_dir}/pre_compact.py",
        "timeout": 30,
    },
    "PostCompact": {
        "command": f"python3 {hooks_dir}/post_compact.py",
        "timeout": 30,
    },
}

settings.setdefault("hooks", {})

for event, hook_cfg in hook_defs.items():
    hook_entry = {"type": "command", **hook_cfg}

    if event not in settings["hooks"]:
        settings["hooks"][event] = [{"hooks": [hook_entry]}]
    else:
        # Check if cit hook already registered (by checking command substring)
        existing = settings["hooks"][event]
        already = False
        for matcher in existing:
            for h in matcher.get("hooks", []):
                if "cit/hooks/" in h.get("command", ""):
                    # Update to new path
                    h["command"] = hook_cfg["command"]
                    h["timeout"] = hook_cfg["timeout"]
                    already = True
        if not already:
            existing.append({"hooks": [hook_entry]})

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)

print("  OK — registered SessionStart, PreCompact, PostCompact hooks")
PYEOF
fi

# ── 4. Register MCP server for Codex ──
if [[ "$TARGET" == "codex" || "$TARGET" == "both" ]]; then
    if command -v codex &> /dev/null; then
        echo "Registering Codex MCP server..."
        PYTHON_PATH="$CIT_DIR/.venv/bin/python"
        if codex mcp get cit &> /dev/null; then
            codex mcp remove cit &> /dev/null || true
        fi
        codex mcp add cit --env CIT_RUNTIME=codex -- "$PYTHON_PATH" -m cit.server
        echo "  OK — registered cit MCP server for Codex"
    else
        echo "⚠️  codex CLI not found — skipping Codex MCP registration"
    fi
fi

# ── 5. Install tmux-aware Codex wrapper ──
if [[ "$TARGET" == "codex" || "$TARGET" == "both" ]]; then
    echo "Installing codex-tmux wrapper..."
    mkdir -p "$HOME/.local/bin"
    cp "$CIT_DIR/scripts/codex-tmux" "$HOME/.local/bin/codex-tmux"
    chmod +x "$HOME/.local/bin/codex-tmux"
    echo "  OK — installed ~/.local/bin/codex-tmux"
fi

echo ""
echo "=== Setup complete! ==="
echo ""
if [[ "$TARGET" == "claude" || "$TARGET" == "both" ]]; then
    echo "Restart Claude Code to activate. Then try:"
    echo "  cit log"
    echo "  cit branch my-topic"
    echo "  cit squash"
    echo ""
fi
if [[ "$TARGET" == "codex" || "$TARGET" == "both" ]]; then
    echo "Restart Codex to activate. Then try:"
    echo "  codex-tmux"
    echo "  cit log"
    echo "  cit branch my-topic"
    echo "  cit squash"
fi
