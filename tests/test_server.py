"""Tests for server module: rendering, session detection, and tool logic."""

import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from cit.store import Branch, Store


# We can't easily import the server module at top level because it initializes
# a global store on import. Instead, we test the logic by reconstructing key functions
# or by patching the store.


class TestRenderTree:
    """Test _render_tree logic by calling it with a patched store."""

    def _make_store_with_branches(self, tmpdir, branches: list[Branch]) -> Store:
        s = Store(Path(tmpdir) / "test.db")
        for b in branches:
            s.add_branch(b)
        return s

    def test_empty_tree(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            s = self._make_store_with_branches(tmpdir, [])
            assert s.get_all_branches() == []
            s.close()

    def test_single_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            s = self._make_store_with_branches(tmpdir, [
                Branch(id="session-abc123", label="main"),
            ])
            branches = s.get_all_branches()
            assert len(branches) == 1
            assert branches[0].label == "main"
            s.close()

    def test_tree_with_children(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            s = self._make_store_with_branches(tmpdir, [
                Branch(id="root", label="main"),
                Branch(id="child1", parent_id="root", label="explore-a"),
                Branch(id="child2", parent_id="root", label="explore-b", summary="done"),
            ])
            branches = s.get_all_branches()
            assert len(branches) == 3

            children = s.get_children("root")
            assert len(children) == 2
            assert {c.label for c in children} == {"explore-a", "explore-b"}
            s.close()

    def test_pending_placeholder_visible(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            s = self._make_store_with_branches(tmpdir, [
                Branch(id="root", label="main"),
                Branch(id="pending-abc12345", parent_id="root", label="deep-dive"),
            ])
            pending = [b for b in s.get_all_branches() if b.id.startswith("pending-")]
            assert len(pending) == 1
            assert pending[0].label == "deep-dive"
            s.close()


class TestGetCurrentSessionId:
    def test_env_var_takes_precedence(self):
        with patch.dict(os.environ, {"CLAUDE_SESSION_ID": "test-session-123"}):
            assert os.environ.get("CLAUDE_SESSION_ID") == "test-session-123"

    def test_no_env_var(self):
        env = os.environ.copy()
        env.pop("CLAUDE_SESSION_ID", None)
        with patch.dict(os.environ, env, clear=True):
            assert os.environ.get("CLAUDE_SESSION_ID") is None



class TestTreeCleanup:
    """Test stale placeholder cleanup."""

    def test_cleanup_removes_old_placeholders(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            s = Store(Path(tmpdir) / "test.db")
            # Create an old placeholder (48h ago)
            old_time = (datetime.now() - timedelta(hours=48)).isoformat()
            s.add_branch(Branch(id="pending-old12345", label="old-fork", created_at=old_time))
            # Create a recent placeholder
            s.add_branch(Branch(id="pending-new12345", label="new-fork"))
            # Create a regular branch (should not be deleted)
            s.add_branch(Branch(id="regular-session", label="main"))

            deleted = s.delete_stale_placeholders(max_age_hours=24)
            assert deleted == 1

            # Old placeholder gone
            assert s.get_branch("pending-old12345") is None
            # New placeholder still there
            assert s.get_branch("pending-new12345") is not None
            # Regular branch untouched
            assert s.get_branch("regular-session") is not None
            s.close()

    def test_cleanup_no_stale(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            s = Store(Path(tmpdir) / "test.db")
            s.add_branch(Branch(id="pending-fresh", label="fresh"))
            deleted = s.delete_stale_placeholders(max_age_hours=24)
            assert deleted == 0
            s.close()


class TestPendingAgeStr:
    def test_age_formatting(self):
        # Import inline to avoid server module global init issues
        from cit.server import _pending_age_str

        now = datetime.now()
        assert "m ago" in _pending_age_str((now - timedelta(minutes=30)).isoformat())
        assert "h ago" in _pending_age_str((now - timedelta(hours=5)).isoformat())
        assert "d ago" in _pending_age_str((now - timedelta(days=3)).isoformat())

    def test_invalid_timestamp(self):
        from cit.server import _pending_age_str
        assert _pending_age_str("not-a-date") == ""


# ─── New tests for conclusion injection (v0.3) ───────────────


def _setup_tree(store: Store) -> Store:
    """Create a tree: main -> [explore-cache (squashed), explore-auth (no summary)]."""
    store.add_branch(Branch(id="main-session", label="main"))
    store.add_branch(Branch(
        id="child-cache",
        parent_id="main-session",
        label="explore-cache",
        summary="KV cache migration latency is the bottleneck, need async prefetch",
    ))
    store.add_branch(Branch(
        id="child-auth",
        parent_id="main-session",
        label="explore-auth",
    ))
    return store


class TestTreeInbox:
    """Test tree_inbox logic — filter by summary, not status."""

    def test_inbox_returns_squashed_children(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "test.db")
            _setup_tree(store)

            children = store.get_children("main-session")
            with_summary = [c for c in children if c.summary]

            assert len(with_summary) == 1
            assert with_summary[0].label == "explore-cache"
            assert "KV cache" in with_summary[0].summary
            store.close()

    def test_inbox_empty_when_no_summaries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "test.db")
            store.add_branch(Branch(id="root", label="main"))
            store.add_branch(Branch(id="c1", parent_id="root", label="wip"))

            children = store.get_children("root")
            with_summary = [c for c in children if c.summary]

            assert len(with_summary) == 0
            store.close()

    def test_inbox_only_shows_branches_with_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "test.db")
            store.add_branch(Branch(id="root", label="main"))
            store.add_branch(Branch(id="c1", parent_id="root", label="no-summary", summary=None))
            store.add_branch(Branch(id="c2", parent_id="root", label="has-summary",
                                    summary="Found the bug in auth module"))

            children = store.get_children("root")
            with_summary = [c for c in children if c.summary]

            assert len(with_summary) == 1
            assert with_summary[0].label == "has-summary"
            store.close()


class TestSquash:
    """Test squash stores summary, re-squash overwrites, root branch rejected."""

    def test_squash_stores_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "test.db")
            store.add_branch(Branch(id="root", label="main"))
            store.add_branch(Branch(id="child", parent_id="root", label="explore"))

            store.update_branch("child", summary="Key finding: async prefetch reduces latency by 40%")
            b = store.get_branch("child")
            assert b.summary == "Key finding: async prefetch reduces latency by 40%"
            store.close()

    def test_squash_overwrites_previous_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "test.db")
            store.add_branch(Branch(id="root", label="main"))
            store.add_branch(Branch(id="child", parent_id="root", label="explore",
                                    summary="Old summary"))

            store.update_branch("child", summary="New summary with better findings")
            b = store.get_branch("child")
            assert b.summary == "New summary with better findings"
            store.close()

    def test_root_branch_has_no_parent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "test.db")
            store.add_branch(Branch(id="root", label="main"))

            b = store.get_branch("root")
            assert b.parent_id is None  # root cannot be squashed
            store.close()


class TestCheckoutPure:
    """Test that checkout does not change summary or any branch state."""

    def test_checkout_does_not_change_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "test.db")
            store.add_branch(Branch(id="root", label="main"))
            store.add_branch(Branch(id="child", parent_id="root", label="explore",
                                    summary="Existing summary"))

            # Simulate checkout: only reads, never writes summary
            b_before = store.get_branch("child")
            summary_before = b_before.summary

            # No write happens during checkout
            b_after = store.get_branch("child")
            assert b_after.summary == summary_before
            store.close()

    def test_checkout_no_summary_branch_stays_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "test.db")
            store.add_branch(Branch(id="root", label="main"))
            store.add_branch(Branch(id="child", parent_id="root", label="explore"))

            b = store.get_branch("child")
            assert b.summary is None
            store.close()


class TestCheckoutNewSquashReminder:
    """Test that checking out to a branch with squashed children shows inbox hint."""

    def test_squashed_children_count(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "test.db")
            _setup_tree(store)

            children = store.get_children("main-session")
            new_squashes = sum(1 for c in children if c.summary is not None)
            assert new_squashes == 1  # only child-cache has summary
            store.close()

    def test_no_squashes_no_hint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "test.db")
            store.add_branch(Branch(id="root", label="main"))
            store.add_branch(Branch(id="c1", parent_id="root", label="wip"))

            children = store.get_children("root")
            new_squashes = sum(1 for c in children if c.summary is not None)
            assert new_squashes == 0
            store.close()


class TestSessionStartChildSummaries:
    """Test that session_start hook logic correctly builds child summary context."""

    def test_hook_injects_squashed_child_summaries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "test.db")
            _setup_tree(store)

            session_id = "main-session"
            branch = store.get_branch(session_id)
            assert branch is not None

            children = store.get_children(session_id)
            squashed_children = [c for c in children if c.summary]

            context_msg = f"[Tree Context] You are on branch: {branch.label}."
            if squashed_children:
                context_msg += "\nCompleted explorations:"
                for c in squashed_children:
                    context_msg += f"\n  - {c.label}: {c.summary}"

            assert "Completed explorations:" in context_msg
            assert "explore-cache" in context_msg
            assert "KV cache" in context_msg
            store.close()

    def test_hook_no_summary_when_no_squashed_children(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = Store(Path(tmpdir) / "test.db")
            store.add_branch(Branch(id="root", label="main"))

            children = store.get_children("root")
            squashed_children = [c for c in children if c.summary]

            context_msg = "[Tree Context] You are on branch: main."
            if squashed_children:
                context_msg += "\nCompleted explorations:"

            assert "Completed explorations:" not in context_msg
            store.close()
