#!/usr/bin/env python3
"""PreCompact hook: snapshot transcript before compaction."""

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from cit.store import Store

DB_PATH = Path(os.environ.get("CIT_DB", Path(__file__).parent.parent / "data" / "cit.db"))
SNAPSHOTS_DIR = Path(os.environ.get("CIT_SNAPSHOTS", Path(__file__).parent.parent / "data" / "snapshots"))


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    session_id = hook_input.get("session_id") or os.environ.get("CLAUDE_SESSION_ID")
    transcript_path = hook_input.get("transcript_path")

    if not session_id:
        return

    # Snapshot the transcript file
    if transcript_path:
        src = Path(transcript_path)
        if src.exists():
            SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            dst = SNAPSHOTS_DIR / f"{session_id}_{ts}.jsonl"
            shutil.copy2(src, dst)

    # Record metric
    store = Store(DB_PATH)
    store.record_metric(session_id, "pre_compact", {
        "trigger": hook_input.get("trigger", "unknown"),
        "timestamp": datetime.now().isoformat(),
    }, runtime="claude")
    store.close()


if __name__ == "__main__":
    main()
