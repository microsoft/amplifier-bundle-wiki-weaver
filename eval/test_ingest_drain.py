"""Drain-loop ingest tests (eval gate for the inbox drain-loop change).

Tests A, B, C cover the new drain-loop behaviour for ``cmd_ingest`` (inbox mode).
Test R is a regression guard: ``--source <file>`` single-file path is unchanged.

All tests use a mocked ``run_inner`` — fast, deterministic, NO real LLM calls.

SAFETY: uses isolated tmp_path dirs only; never touches live wiki runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from wiki_weaver.wiki_weaver import ARCHIVE, FAILED, INBOX, cmd_ingest  # noqa: E402


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_wiki(tmp_path: Path) -> Path:
    """Minimal wiki scaffold that satisfies cmd_ingest's prerequisite checks."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / INBOX).mkdir()
    (wiki / ARCHIVE).mkdir()
    (wiki / ".processed.jsonl").touch()
    return wiki


def _seed_inbox(
    inbox: Path,
    name: str,
    content: str | None = None,
) -> Path:
    """Write *name* to *inbox* with a backdated mtime (clears the 2-s debounce).

    Default content is unique per *name* so that content-hash dedup never
    treats two different seeded files as duplicates of each other.
    """
    if content is None:
        content = f"# {name}\n\nUnique body text for {name}.\n"
    p = inbox / name
    p.write_text(content, encoding="utf-8")
    old = time.time() - 10  # 10 s in the past → well past the 2-s debounce
    os.utime(p, (old, old))
    return p


def _fake_result(converged: bool = True, status: str = "success") -> SimpleNamespace:
    """Minimal stand-in for InnerResult (no engine_runner import needed)."""
    return SimpleNamespace(
        converged=converged,
        status=status,
        failure_reason=None if converged else "did not converge",
        logs_dir=Path("/tmp/fake_logs"),
    )


def _args(
    wiki: Path,
    *,
    source: str | None = None,
    keep_going: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        wiki=str(wiki),
        source=source,
        max_cycles=None,
        keep_going=keep_going,
    )


# ---------------------------------------------------------------------------
# Test A — re-glob picks up files added mid-drain
# ---------------------------------------------------------------------------


def test_a_reglov_picks_up_mid_drain_addition(tmp_path: Path) -> None:
    """Files dropped into _inbox while the drain is running are ingested.

    Scenario:
      - Start with 2 files (a.md, b.md).
      - The mock drops a 3rd file (c.md) on its first call (simulating a
        concurrent producer adding a file while a.md is being processed).
      - All 3 must end up in _archive/; inbox must be empty; loop terminates.
    """
    wiki = _make_wiki(tmp_path)
    inbox = wiki / INBOX
    archive = wiki / ARCHIVE

    _seed_inbox(inbox, "a.md")
    _seed_inbox(inbox, "b.md")

    call_count = [0]

    def mock_run(src, wiki_dir, max_cycles, source_id):
        call_count[0] += 1
        assert call_count[0] <= 10, "spin guard: drain loop iterated too many times"
        # On the very first call (processing a.md), simulate a concurrent
        # producer by dropping a 3rd file into the inbox.
        if call_count[0] == 1:
            new_file = inbox / "c.md"
            new_file.write_text("# Mid-drain addition\n\nBody.\n", encoding="utf-8")
            # Backdate so the debounce passes on the next glob.
            old = time.time() - 10
            os.utime(new_file, (old, old))
        return _fake_result(True)

    with patch("wiki_weaver.engine_runner.run_inner", side_effect=mock_run):
        rc = cmd_ingest(_args(wiki))

    assert rc == 0, "all sources converged — exit code should be 0"
    assert call_count[0] == 3, (
        f"expected 3 run_inner calls (a, b, c); got {call_count[0]}"
    )

    remaining = list(inbox.glob("*.md"))
    assert not remaining, f"inbox should be empty; found: {[p.name for p in remaining]}"

    assert (archive / "a.md").exists(), "a.md must be in _archive/"
    assert (archive / "b.md").exists(), "b.md must be in _archive/"
    assert (archive / "c.md").exists(), "c.md must be in _archive/"


# ---------------------------------------------------------------------------
# Test B — engine error → _failed/; drain continues; exits nonzero
# ---------------------------------------------------------------------------


def test_b_failed_source_routes_to_failed_dir(tmp_path: Path) -> None:
    """An engine error on one source sends it to _failed/; others still succeed.

    Scenario:
      - 3 files: a.md (succeeds), b.md (raises), c.md (succeeds).
      - b.md must land in _failed/.
      - a.md and c.md must land in _archive/.
      - Inbox must be empty (loop terminates; no spin).
      - Exit code must be nonzero (fail-loud after drain).
    """
    wiki = _make_wiki(tmp_path)
    inbox = wiki / INBOX
    archive = wiki / ARCHIVE

    _seed_inbox(inbox, "a.md")
    _seed_inbox(inbox, "b.md")  # this one will raise
    _seed_inbox(inbox, "c.md")

    call_count = [0]

    def mock_run(src, wiki_dir, max_cycles, source_id):
        call_count[0] += 1
        assert call_count[0] <= 10, "spin guard: drain loop iterated too many times"
        if src.name == "b.md":
            raise RuntimeError("simulated engine failure")
        return _fake_result(True)

    with patch("wiki_weaver.engine_runner.run_inner", side_effect=mock_run):
        rc = cmd_ingest(_args(wiki))

    # Inbox must be empty — all 3 files must have been dispatched.
    remaining = list(inbox.glob("*.md"))
    assert not remaining, f"inbox should be empty; found: {[p.name for p in remaining]}"

    # Successful files land in _archive/.
    assert (archive / "a.md").exists(), "a.md must be in _archive/"
    assert (archive / "c.md").exists(), "c.md must be in _archive/"

    # Failed file lands in _failed/ (name may have a suffix due to collision-safe move).
    failed_dir = wiki / FAILED
    assert failed_dir.exists(), "_failed/ directory must be created"
    failed_files = list(failed_dir.glob("b*.md"))
    assert failed_files, "_failed/ should contain b.md (or a b*.md variant)"

    # Exit code must be nonzero.
    assert rc != 0, "at least one failure → exit code must be nonzero"


# ---------------------------------------------------------------------------
# Test C — duplicate is cleared from inbox without calling run_inner
# ---------------------------------------------------------------------------


def test_c_duplicate_cleared_from_inbox_no_spin(tmp_path: Path) -> None:
    """A file whose hash is already marked ingested in the registry must be
    moved out of _inbox (to _archive/) without calling run_inner.

    The drain loop must terminate (not spin on the dup indefinitely).
    """
    wiki = _make_wiki(tmp_path)
    inbox = wiki / INBOX
    archive = wiki / ARCHIVE

    content = "# Already ingested\n\nThis content was previously processed.\n"
    dup_file = _seed_inbox(inbox, "dup.md", content)

    # Pre-populate .sources.json with an entry marked ingested=True.
    file_hash = hashlib.sha256(dup_file.read_bytes()).hexdigest()
    registry = {
        "version": 1,
        "next_id": 2,
        "sources": [
            {
                "id": 1,
                "filename": "dup.md",
                "hash": file_hash,
                "ingested": True,
                "first_seen": "2026-01-01T00:00:00",
            }
        ],
    }
    (wiki / ".sources.json").write_text(
        json.dumps(registry, indent=2), encoding="utf-8"
    )

    with patch("wiki_weaver.engine_runner.run_inner") as mock_run:
        rc = cmd_ingest(_args(wiki))

    # run_inner must NOT be called for a duplicate.
    mock_run.assert_not_called()

    assert rc == 0, "no engine errors — exit code should be 0"

    # Inbox must be empty — dup cleared out.
    remaining = list(inbox.glob("*.md"))
    assert not remaining, f"inbox should be empty; found: {[p.name for p in remaining]}"

    # Dup must have been moved to _archive/ (name may carry a suffix).
    archived = list(archive.glob("dup*.md"))
    assert archived, (
        f"_archive/ should contain the dup file; found: {list(archive.iterdir())}"
    )


# ---------------------------------------------------------------------------
# Test R — regression: --source <file> single-file path is unchanged
# ---------------------------------------------------------------------------


def test_r_single_source_path_regression(tmp_path: Path) -> None:
    """The --source <file> single-file path retains its original behaviour.

    Key invariants:
      - run_inner is called exactly once.
      - The source file (outside the wiki inbox) is NOT moved.
      - Exit code is 0 on success.
    """
    wiki = _make_wiki(tmp_path)

    # Source file lives OUTSIDE the wiki (typical single-file invocation).
    source_file = tmp_path / "external.md"
    source_file.write_text("# External Source\n\nBody text.\n", encoding="utf-8")

    with patch(
        "wiki_weaver.engine_runner.run_inner", return_value=_fake_result(True)
    ) as mock_run:
        rc = cmd_ingest(_args(wiki, source=str(source_file)))

    assert rc == 0, "success path should return 0"
    mock_run.assert_called_once()

    # File outside inbox must not be moved.
    assert source_file.exists(), "--source file must remain at its original location"
