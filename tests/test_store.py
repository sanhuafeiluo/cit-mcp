"""Tests for the branch store."""

import tempfile
from pathlib import Path

from cit.store import Branch, Store


class TestStore:
    def _make_store(self, tmpdir):
        return Store(Path(tmpdir) / "test.db")

    def test_add_and_get_branch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._make_store(tmpdir)
            b = Branch(id="session-1", label="main")
            store.add_branch(b)
            loaded = store.get_branch("session-1")
            assert loaded is not None
            assert loaded.label == "main"
            store.close()

    def test_parent_child(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._make_store(tmpdir)
            store.add_branch(Branch(id="root", label="main"))
            store.add_branch(Branch(id="child-1", parent_id="root", label="explore-cache"))
            store.add_branch(Branch(id="child-2", parent_id="root", label="explore-auth"))

            children = store.get_children("root")
            assert len(children) == 2
            assert {c.label for c in children} == {"explore-cache", "explore-auth"}
            store.close()

    def test_roots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._make_store(tmpdir)
            store.add_branch(Branch(id="root1", label="project-a"))
            store.add_branch(Branch(id="root2", label="project-b"))
            store.add_branch(Branch(id="child", parent_id="root1", label="fork"))

            roots = store.get_roots()
            assert len(roots) == 2
            store.close()

    def test_update_branch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._make_store(tmpdir)
            store.add_branch(Branch(id="s1", label="test"))
            store.update_branch("s1", summary="done exploring")
            b = store.get_branch("s1")
            assert b.summary == "done exploring"
            store.close()

    def test_find_by_prefix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._make_store(tmpdir)
            store.add_branch(Branch(id="abc123", label="explore-cache"))
            store.add_branch(Branch(id="def456", label="explore-auth"))

            # Find by label prefix
            assert store.find_branch_by_prefix("explore-c") == "abc123"
            # Find by id prefix
            assert store.find_branch_by_prefix("def") == "def456"
            # Not found
            assert store.find_branch_by_prefix("zzz") is None
            store.close()

    def test_metrics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._make_store(tmpdir)
            store.record_metric("s1", "fork", {"label": "test"})
            store.record_metric("s1", "checkout", {"to": "s2"})
            store.record_metric("s2", "checkout", {"from": "s1"})

            m1 = store.get_metrics("s1")
            assert len(m1) == 2
            all_m = store.get_metrics()
            assert len(all_m) == 3
            store.close()
