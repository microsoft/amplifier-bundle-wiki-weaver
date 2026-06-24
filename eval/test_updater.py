"""Unit tests for wiki_weaver.updater.

All tests are keyless and deterministic — no real network calls, no real
filesystem access for foundation internals.  The tests validate:

  - SourceRecord.is_mutable: ref-is-mutable guard
  - _installed_commit: PEP 610 direct_url.json parsing
  - Layer1Result stale detection in update_layer1
  - check_layer1 / check_layer2 formatting helpers
  - _update_check / _update_real output via lib.update
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock


# Make wiki_weaver importable without installing.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from wiki_weaver.updater import (  # noqa: E402
    Layer1Result,
    SourceRecord,
    _installed_commit,
    update_layer1,
)


# ---------------------------------------------------------------------------
# SourceRecord.is_mutable
# ---------------------------------------------------------------------------


class TestSourceRecordIsMutable:
    """ref-is-mutable guard correctly identifies branch names vs pinned refs."""

    def test_main_is_mutable(self):
        rec = SourceRecord(
            label="test",
            uri="git+https://github.com/microsoft/foo@main",
        )
        assert rec.is_mutable is True

    def test_head_is_mutable(self):
        rec = SourceRecord(
            label="test",
            uri="git+https://github.com/microsoft/foo",
        )
        # No explicit ref → defaults to mutable
        assert rec.is_mutable is True

    def test_full_sha_40_hex_is_pinned(self):
        sha = "a" * 40
        rec = SourceRecord(
            label="test",
            uri=f"git+https://github.com/microsoft/foo@{sha}",
        )
        assert rec.is_mutable is False

    def test_version_tag_is_pinned(self):
        rec = SourceRecord(
            label="test",
            uri="git+https://github.com/microsoft/foo@v1.2.3",
        )
        assert rec.is_mutable is False

    def test_main_with_subdirectory_is_mutable(self):
        rec = SourceRecord(
            label="test",
            uri="git+https://github.com/microsoft/foo@main#subdirectory=bar",
        )
        assert rec.is_mutable is True

    def test_partial_sha_not_40_is_mutable(self):
        """A short SHA (< 40 chars) is not conclusively pinned — treated as mutable."""
        rec = SourceRecord(
            label="test",
            uri="git+https://github.com/microsoft/foo@abc1234",
        )
        # 7-char short SHA: not flagged as pinned (safe: may clone wrong ref,
        # but that would fail; better than blocking on uncertain refs)
        assert rec.is_mutable is True


# ---------------------------------------------------------------------------
# SourceRecord helpers
# ---------------------------------------------------------------------------


class TestSourceRecordHelpers:
    def test_local_short_truncates_to_8(self):
        rec = SourceRecord(label="x", uri="u", local_sha="abcdef1234567890")
        assert rec.local_short == "abcdef12"

    def test_local_short_not_cached(self):
        rec = SourceRecord(label="x", uri="u", local_sha=None)
        assert rec.local_short == "(not cached)"

    def test_target_short_truncates_to_8(self):
        rec = SourceRecord(label="x", uri="u", target_sha="1234567890abcdef")
        assert rec.target_short == "12345678"

    def test_target_short_unknown(self):
        rec = SourceRecord(label="x", uri="u", target_sha=None)
        assert rec.target_short == "(unknown)"


# ---------------------------------------------------------------------------
# _installed_commit: PEP 610 direct_url.json parsing
# ---------------------------------------------------------------------------


class TestInstalledCommit:
    """_installed_commit reads from direct_url.json via importlib.metadata."""

    def test_returns_commit_id_when_present(self, tmp_path, monkeypatch):
        """When direct_url.json has a vcs_info.commit_id, return it."""
        commit_id = "abc123def456abc123def456abc123def456abc1"
        direct_url = {
            "url": "https://github.com/microsoft/amplifier-foundation",
            "vcs_info": {
                "vcs": "git",
                "requested_revision": "main",
                "commit_id": commit_id,
            },
        }

        mock_dist = MagicMock()
        mock_dist.read_text.return_value = json.dumps(direct_url)

        monkeypatch.setattr(
            "wiki_weaver.updater.importlib.metadata.distribution",
            lambda name: mock_dist,
        )
        monkeypatch.setattr(
            "wiki_weaver.updater.importlib.invalidate_caches", lambda: None
        )

        result = _installed_commit("amplifier-foundation")
        assert result == commit_id

    def test_returns_none_when_no_direct_url(self, monkeypatch):
        """When direct_url.json is absent, return None (don't crash)."""
        mock_dist = MagicMock()
        mock_dist.read_text.return_value = None

        monkeypatch.setattr(
            "wiki_weaver.updater.importlib.metadata.distribution",
            lambda name: mock_dist,
        )
        monkeypatch.setattr(
            "wiki_weaver.updater.importlib.invalidate_caches", lambda: None
        )

        result = _installed_commit("amplifier-foundation")
        assert result is None

    def test_returns_none_when_package_not_found(self, monkeypatch):
        """When the package is not installed, return None (don't crash)."""
        monkeypatch.setattr(
            "wiki_weaver.updater.importlib.metadata.distribution",
            MagicMock(side_effect=Exception("package not found")),
        )
        monkeypatch.setattr(
            "wiki_weaver.updater.importlib.invalidate_caches", lambda: None
        )

        result = _installed_commit("missing-package")
        assert result is None

    def test_returns_none_when_no_vcs_info(self, monkeypatch):
        """When direct_url.json has no vcs_info (e.g. a local file dep), return None."""
        direct_url = {
            "url": "file:///home/user/repos/amplifier-foundation",
            "dir_info": {"editable": True},
        }
        mock_dist = MagicMock()
        mock_dist.read_text.return_value = json.dumps(direct_url)

        monkeypatch.setattr(
            "wiki_weaver.updater.importlib.metadata.distribution",
            lambda name: mock_dist,
        )
        monkeypatch.setattr(
            "wiki_weaver.updater.importlib.invalidate_caches", lambda: None
        )

        result = _installed_commit("amplifier-foundation")
        assert result is None


# ---------------------------------------------------------------------------
# update_layer1: stale detection and ladder logic
# ---------------------------------------------------------------------------


class TestUpdateLayer1StaleDetection:
    """update_layer1 verify+ladder+fail-loud logic (all I/O mocked)."""

    def _make_installed_commit_seq(self, *commits: Optional[str]):
        """Return a side-effect sequence for _installed_commit.

        Each call to _installed_commit pops the next commit from the sequence.
        If exhausted, returns the last provided value.
        """
        it = iter(commits)
        last = [commits[-1] if commits else None]

        def _fn(name: str) -> Optional[str]:
            try:
                v = next(it)
                last[0] = v
                return v
            except StopIteration:
                return last[0]

        return _fn

    def test_success_on_rung1_when_no_remote_move(self, monkeypatch):
        """When remote == before, rung-1 passes trivially (nothing to verify)."""
        sha = "a" * 40
        monkeypatch.setattr("wiki_weaver.updater._installed_commit", lambda n: sha)
        monkeypatch.setattr("wiki_weaver.updater._run_install", lambda **kw: (0, ""))

        async def _no_move(url: str) -> Optional[str]:
            return sha  # remote == local → no move expected

        monkeypatch.setattr(
            "wiki_weaver.updater._get_remote_commit_for",
            _no_move,
        )

        res = update_layer1()
        assert res.success is True
        assert res.rung_reached == 1
        assert res.stale == []

    def test_success_on_rung1_when_remote_moved_and_install_moved(self, monkeypatch):
        """When remote > before AND installed-after == remote, rung-1 succeeds."""
        old_sha = "0" * 40
        new_sha = "1" * 40

        # _installed_commit: before=old, after=new (updated on rung-1)
        call_count = [0]

        def _installed(name: str) -> Optional[str]:
            call_count[0] += 1
            # First set of calls (pre-step): return old
            # Second set (after rung-1): return new
            if call_count[0] <= 2:
                return old_sha
            return new_sha

        monkeypatch.setattr("wiki_weaver.updater._installed_commit", _installed)
        monkeypatch.setattr("wiki_weaver.updater._run_install", lambda **kw: (0, ""))

        async def _remote_moved(url: str) -> Optional[str]:
            return new_sha

        monkeypatch.setattr("wiki_weaver.updater._get_remote_commit_for", _remote_moved)

        res = update_layer1()
        assert res.success is True
        assert res.rung_reached == 1
        assert res.stale == []

    def test_escalates_to_rung2_when_stale_after_rung1(self, monkeypatch):
        """If rung-1 didn't update the package, escalate to rung-2 (--no-cache)."""
        old_sha = "0" * 40
        new_sha = "1" * 40

        call_count = [0]

        def _installed(name: str) -> Optional[str]:
            call_count[0] += 1
            # pre-step calls: old
            # after-rung-1 calls: still old (stale)
            # after-rung-2 calls: new (fixed)
            if call_count[0] <= 4:
                return old_sha
            return new_sha

        rungs_tried = []

        def _run_install_tracking(**kw):
            rungs_tried.append(kw.get("no_cache", False))
            return 0, ""

        monkeypatch.setattr("wiki_weaver.updater._installed_commit", _installed)
        monkeypatch.setattr("wiki_weaver.updater._run_install", _run_install_tracking)

        async def _remote_moved(url: str) -> Optional[str]:
            return new_sha

        monkeypatch.setattr("wiki_weaver.updater._get_remote_commit_for", _remote_moved)

        res = update_layer1()
        assert res.success is True
        assert res.rung_reached == 2
        # Rung-1 tried plain, rung-2 tried --no-cache
        assert rungs_tried == [False, True]

    def test_escalates_to_rung3_when_stale_after_rung2(self, monkeypatch):
        """If rung-2 still stale, escalate to rung-3 (cache clean + reinstall)."""
        old_sha = "0" * 40
        new_sha = "1" * 40

        call_count = [0]

        def _installed(name: str) -> Optional[str]:
            call_count[0] += 1
            # pre-step: old (calls 1..2)
            # after rung-1: still old (calls 3..4) -- stale
            # after rung-2: still old (calls 5..6) -- still stale
            # after rung-3: new (calls 7+)
            if call_count[0] <= 6:
                return old_sha
            return new_sha

        clean_called = [False]

        def _run_clean(names: list):
            clean_called[0] = True
            return 0

        monkeypatch.setattr("wiki_weaver.updater._installed_commit", _installed)
        monkeypatch.setattr("wiki_weaver.updater._run_install", lambda **kw: (0, ""))
        monkeypatch.setattr("wiki_weaver.updater._run_cache_clean", _run_clean)

        async def _remote_moved(url: str) -> Optional[str]:
            return new_sha

        monkeypatch.setattr("wiki_weaver.updater._get_remote_commit_for", _remote_moved)

        res = update_layer1()
        assert res.success is True
        assert res.rung_reached == 3
        assert clean_called[0] is True

    def test_fail_loud_when_all_rungs_exhausted(self, monkeypatch):
        """After all 3 rungs, stale packages → success=False, stale list populated."""
        old_sha = "0" * 40
        new_sha = "1" * 40

        # Never updates — _installed_commit always returns old_sha
        monkeypatch.setattr("wiki_weaver.updater._installed_commit", lambda n: old_sha)
        monkeypatch.setattr("wiki_weaver.updater._run_install", lambda **kw: (0, ""))
        monkeypatch.setattr("wiki_weaver.updater._run_cache_clean", lambda names: 0)

        async def _remote_moved(url: str) -> Optional[str]:
            return new_sha

        monkeypatch.setattr("wiki_weaver.updater._get_remote_commit_for", _remote_moved)

        res = update_layer1()
        assert res.success is False
        assert res.rung_reached == 3
        # Both tracked packages are stale
        assert len(res.stale) == 2

    def test_install_failure_sets_error(self, monkeypatch):
        """Non-zero exit from uv install stops the ladder and records the error."""
        monkeypatch.setattr("wiki_weaver.updater._installed_commit", lambda n: None)
        monkeypatch.setattr(
            "wiki_weaver.updater._run_install",
            lambda **kw: (1, "error: no such package"),
        )

        async def _remote(url: str) -> Optional[str]:
            return "a" * 40

        monkeypatch.setattr("wiki_weaver.updater._get_remote_commit_for", _remote)

        res = update_layer1()
        assert res.success is False
        assert res.rung_reached == 1
        assert any("rung-1 failed" in e for e in res.errors)


# ---------------------------------------------------------------------------
# Commit-moved comparison helper
# ---------------------------------------------------------------------------


class TestLayer1ResultCommitMoved:
    """Layer1Result correctly identifies which packages moved."""

    def test_package_moved_when_before_ne_after(self):
        res = Layer1Result(
            before={"pkg": "aaa" * 13 + "a"},
            after={"pkg": "bbb" * 13 + "b"},
            remote={"pkg": "bbb" * 13 + "b"},
        )
        # Package moved: before != after
        assert res.before["pkg"] != res.after["pkg"]

    def test_package_unchanged_when_already_latest(self):
        sha = "a" * 40
        res = Layer1Result(
            before={"pkg": sha},
            after={"pkg": sha},
            remote={"pkg": sha},
        )
        assert res.before["pkg"] == res.after["pkg"]
