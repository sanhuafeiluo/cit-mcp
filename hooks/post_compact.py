#!/usr/bin/env python3
"""PostCompact hook: record compact summary back to the branch."""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from cit.store import Store

DB_PATH = Path(os.environ.get("CIT_DB", Path(__file__).parent.parent / "data" / "cit.db"))


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    session_id = hook_input.get("session_id") or os.environ.get("CLAUDE_SESSION_ID")
    compact_summary = hook_input.get("compact_summary", "")

    if not session_id:
        return

    store = Store(DB_PATH)

    # Record metric
    store.record_metric(session_id, "post_compact", {
        "summary_length": len(compact_summary),
        "timestamp": datetime.now().isoformat(),
    }, runtime="claude")

    # If branch exists, update its summary
    branch = store.get_branch(session_id, runtime="claude")
    if branch and compact_summary:
        store.update_branch(session_id, runtime="claude", summary=compact_summary)

    store.close()


if __name__ == "__main__":
    main()
