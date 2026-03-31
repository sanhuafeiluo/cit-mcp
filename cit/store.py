"""SQLite persistence for branch tree and metrics."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "cit.db"


@dataclass
class Branch:
    id: str  # session_id from runtime
    parent_id: Optional[str] = None
    label: str = ""
    fork_point_msg_index: int = 0
    summary: Optional[str] = None
    created_at: str = ""
    tmux_pane_id: Optional[str] = None  # e.g. "%5" — tmux unique pane ID
    runtime: str = "claude"

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        if not self.runtime:
            self.runtime = "claude"


class Store:
    """SQLite-backed storage for the branch tree."""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS branches (
                id TEXT PRIMARY KEY,
                parent_id TEXT,
                label TEXT DEFAULT '',
                fork_point_msg_index INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                summary TEXT,
                created_at TEXT,
                token_count INTEGER DEFAULT 0,
                cache_hit_rate REAL DEFAULT 0.0,
                tmux_pane_id TEXT,
                runtime TEXT DEFAULT 'claude'
            );

            CREATE TABLE IF NOT EXISTS metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                data TEXT,
                timestamp TEXT,
                runtime TEXT DEFAULT 'claude'
            );

            CREATE INDEX IF NOT EXISTS idx_branches_parent ON branches(parent_id);
            CREATE INDEX IF NOT EXISTS idx_branches_runtime_parent ON branches(runtime, parent_id);
            CREATE INDEX IF NOT EXISTS idx_metrics_session ON metrics(session_id);
            CREATE INDEX IF NOT EXISTS idx_metrics_runtime_session ON metrics(runtime, session_id);
        """)
        # Migrations: add columns if missing (safe for existing DBs)
        for col, col_type in [
            ("tmux_pane_id", "TEXT"),
            ("runtime", "TEXT DEFAULT 'claude'"),
        ]:
            try:
                self.conn.execute(f"ALTER TABLE branches ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError:
                pass  # column already exists

        for col, col_type in [("runtime", "TEXT DEFAULT 'claude'")]:
            try:
                self.conn.execute(f"ALTER TABLE metrics ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError:
                pass  # column already exists

        # Backfill rows created before runtime support.
        self.conn.execute("UPDATE branches SET runtime = 'claude' WHERE runtime IS NULL OR runtime = ''")
        self.conn.execute("UPDATE metrics SET runtime = 'claude' WHERE runtime IS NULL OR runtime = ''")
        self.conn.commit()

    _BRANCH_FIELDS = {f.name for f in __import__('dataclasses').fields(Branch)}

    def _to_branch(self, row: sqlite3.Row) -> Branch:
        """Convert a DB row to Branch, ignoring columns not in the dataclass."""
        return Branch(**{k: row[k] for k in dict(row) if k in self._BRANCH_FIELDS})

    def add_branch(self, branch: Branch) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO branches
               (id, parent_id, label, fork_point_msg_index, summary, created_at, tmux_pane_id, runtime)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (branch.id, branch.parent_id, branch.label, branch.fork_point_msg_index,
             branch.summary, branch.created_at, branch.tmux_pane_id, branch.runtime),
        )
        self.conn.commit()

    def get_branch(self, branch_id: str, runtime: str | None = None) -> Optional[Branch]:
        if runtime is None:
            row = self.conn.execute("SELECT * FROM branches WHERE id = ?", (branch_id,)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM branches WHERE id = ? AND runtime = ?",
                (branch_id, runtime),
            ).fetchone()
        if not row:
            return None
        return self._to_branch(row)

    def get_children(self, parent_id: str, runtime: str | None = None) -> list[Branch]:
        if runtime is None:
            rows = self.conn.execute(
                "SELECT * FROM branches WHERE parent_id = ? ORDER BY created_at", (parent_id,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM branches WHERE parent_id = ? AND runtime = ? ORDER BY created_at",
                (parent_id, runtime),
            ).fetchall()
        return [self._to_branch(r) for r in rows]

    def get_all_branches(self, runtime: str | None = None) -> list[Branch]:
        if runtime is None:
            rows = self.conn.execute("SELECT * FROM branches ORDER BY created_at").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM branches WHERE runtime = ? ORDER BY created_at", (runtime,)
            ).fetchall()
        return [self._to_branch(r) for r in rows]

    def get_roots(self, runtime: str | None = None) -> list[Branch]:
        if runtime is None:
            rows = self.conn.execute(
                "SELECT * FROM branches WHERE parent_id IS NULL ORDER BY created_at"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM branches WHERE parent_id IS NULL AND runtime = ? ORDER BY created_at",
                (runtime,),
            ).fetchall()
        return [self._to_branch(r) for r in rows]

    _UPDATABLE_COLS = frozenset({"label", "summary", "tmux_pane_id"})

    def update_branch(self, branch_id: str, runtime: str | None = None, **kwargs) -> None:
        if not kwargs:
            return
        bad = set(kwargs) - self._UPDATABLE_COLS
        if bad:
            raise ValueError(f"Cannot update column(s): {bad}")
        set_clause = ", ".join(f"{k} = ?" for k in kwargs)
        if runtime is None:
            values = list(kwargs.values()) + [branch_id]
            self.conn.execute(f"UPDATE branches SET {set_clause} WHERE id = ?", values)
        else:
            values = list(kwargs.values()) + [branch_id, runtime]
            self.conn.execute(
                f"UPDATE branches SET {set_clause} WHERE id = ? AND runtime = ?",
                values,
            )
        self.conn.commit()

    def get_branch_by_pane_id(self, pane_id: str, runtime: str | None = None) -> Optional[Branch]:
        """Find a branch by its tmux pane_id."""
        if runtime is None:
            row = self.conn.execute(
                "SELECT * FROM branches WHERE tmux_pane_id = ? ORDER BY created_at DESC LIMIT 1",
                (pane_id,),
            ).fetchone()
        else:
            row = self.conn.execute(
                """SELECT * FROM branches
                   WHERE tmux_pane_id = ? AND runtime = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (pane_id, runtime),
            ).fetchone()
        return self._to_branch(row) if row else None

    def find_branch_by_prefix(
        self, prefix: str, current_session_id: str | None = None, runtime: str | None = None,
    ) -> Optional[str]:
        """Find branch ID by prefix match on id or label.

        When current_session_id is provided, prefer the parent/sibling of the
        current branch over an arbitrary match (avoids "wrong main" problem
        when multiple branches share the same label prefix).
        """
        # Collect all label matches
        if runtime is None:
            rows = self.conn.execute(
                "SELECT id, parent_id, label FROM branches WHERE label LIKE ?",
                (prefix + "%",),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id, parent_id, label FROM branches WHERE label LIKE ? AND runtime = ?",
                (prefix + "%", runtime),
            ).fetchall()

        if rows:
            # Exact label match always wins
            for r in rows:
                if r["label"] == prefix:
                    return r["id"]

            if len(rows) == 1:
                return rows[0]["id"]

            # Multiple matches — prefer contextually relevant branch
            if current_session_id:
                current = self.get_branch(current_session_id, runtime=runtime)
                if current:
                    # 1st priority: parent of current branch
                    for r in rows:
                        if r["id"] == current.parent_id:
                            return r["id"]
                    # 2nd priority: sibling (same parent)
                    for r in rows:
                        if r["parent_id"] == current.parent_id:
                            return r["id"]

            # Fallback: most recently created match
            return rows[-1]["id"]

        # Then try id prefix
        if runtime is None:
            row = self.conn.execute(
                "SELECT id FROM branches WHERE id LIKE ? LIMIT 1", (prefix + "%",)
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT id FROM branches WHERE id LIKE ? AND runtime = ? LIMIT 1",
                (prefix + "%", runtime),
            ).fetchone()
        return row["id"] if row else None

    def record_metric(
        self, session_id: str, event_type: str, data: dict, runtime: str = "claude",
    ) -> None:
        self.conn.execute(
            """INSERT INTO metrics (session_id, event_type, data, timestamp, runtime)
               VALUES (?, ?, ?, ?, ?)""",
            (session_id, event_type, json.dumps(data), datetime.now().isoformat(), runtime),
        )
        self.conn.commit()

    def get_metrics(self, session_id: str | None = None, runtime: str | None = None) -> list[dict]:
        if session_id and runtime is not None:
            rows = self.conn.execute(
                """SELECT * FROM metrics
                   WHERE session_id = ? AND runtime = ?
                   ORDER BY timestamp""",
                (session_id, runtime),
            ).fetchall()
        elif session_id:
            rows = self.conn.execute(
                "SELECT * FROM metrics WHERE session_id = ? ORDER BY timestamp", (session_id,),
            ).fetchall()
        elif runtime is not None:
            rows = self.conn.execute(
                "SELECT * FROM metrics WHERE runtime = ? ORDER BY timestamp", (runtime,),
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM metrics ORDER BY timestamp").fetchall()
        return [dict(r) for r in rows]

    def delete_stale_placeholders(self, max_age_hours: int = 24, runtime: str | None = None) -> int:
        """Delete pending-* branches older than max_age_hours. Returns count deleted."""
        cutoff = (datetime.now() - timedelta(hours=max_age_hours)).isoformat()
        if runtime is None:
            cursor = self.conn.execute(
                "DELETE FROM branches WHERE id LIKE 'pending-%' AND created_at < ?",
                (cutoff,),
            )
        else:
            cursor = self.conn.execute(
                """DELETE FROM branches
                   WHERE id LIKE 'pending-%' AND created_at < ? AND runtime = ?""",
                (cutoff, runtime),
            )
        self.conn.commit()
        return cursor.rowcount

    def close(self):
        self.conn.close()
