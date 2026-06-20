"""Any-text ingest eval gate (test_any_text_ingest.py).

Tests A, B, C verify the broadened inbox drain loop introduced by the
any-text ingest feature:
  - A: non-md UTF-8 text files (.py, .txt) are ingested normally.
  - B: binary files (NUL byte) route to _failed/ without calling run_inner.
  - C: mixed inbox (.md, .rs, binary, .DS_Store) routes each file correctly.

All tests use a mocked run_inner — fast, deterministic, NO real LLM calls.

SAFETY: uses isolated tmp_path dirs only; never touches live wiki runs.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from wiki_weaver.wiki_weaver import ARCHIVE, FAILED, INBOX, cmd_ingest  # noqa: E402


@pytest.fixture(autouse=True)
def _bypass_env_gate(monkeypatch):
    """Bypass the cmd_ingest env preflight so these tests exercise drain logic.

    The env gate (amplifier_foundation import + ANTHROPIC_API_KEY) is covered by
    eval/test_preflight_gate.py. These tests assume a working environment and
    verify drain ORCHESTRATION (text/binary routing, re-glob, dedup) with a
    mocked run_inner, so they must run regardless of foundation/key presence in
    a lightweight CI env. Patching preflight to return no failures is robust
    whether or not foundation is installed or a key is set.
    """
    monkeypatch.setattr("wiki_weaver.wiki_weaver.preflight", lambda **_kw: [])


# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_ingest_drain.py conventions)
# ---------------------------------------------------------------------------


def _make_wiki(tmp_path: Path) -> Path:
    """Minimal wiki scaffold that satisfies cmd_ingest's prerequisite checks."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / INBOX).mkdir()
    (wiki / ARCHIVE).mkdir()
    (wiki / ".processed.jsonl").touch()
    return wiki


def _seed_text(inbox: Path, name: str, content: str | None = None) -> Path:
    """Write a UTF-8 text file with a backdated mtime (clears the 2-s debounce)."""
    if content is None:
        content = f"# {name}\n\nUnique body text for {name}.\n"
    p = inbox / name
    p.write_text(content, encoding="utf-8")
    old = time.time() - 10
    os.utime(p, (old, old))
    return p


def _seed_binary(inbox: Path, name: str, data: bytes | None = None) -> Path:
    """Write a binary file (contains NUL byte) with a backdated mtime."""
    if data is None:
        # PNG-like header with NUL byte — clearly binary
        data = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x01\x02\x03\x04"
    p = inbox / name
    p.write_bytes(data)
    old = time.time() - 10
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


def _args(wiki: Path, *, source: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        wiki=str(wiki),
        source=source,
        max_cycles=None,
        keep_going=False,
    )


# ---------------------------------------------------------------------------
# Test A — non-.md UTF-8 text files are ingested (→ _archive)
# ---------------------------------------------------------------------------


def test_a_non_md_text_files_are_ingested(tmp_path: Path) -> None:
    """foo.py and notes.txt (valid UTF-8) must be ingested just like .md files.

    Scenario:
      - _inbox/ contains foo.py and notes.txt.
      - Both are valid UTF-8 text; _looks_like_text() returns True for each.
      - run_inner is called once per file.
      - Both land in _archive/; inbox is empty; exit code is 0.
    """
    wiki = _make_wiki(tmp_path)
    inbox = wiki / INBOX
    archive = wiki / ARCHIVE

    _seed_text(inbox, "foo.py", "def hello():\n    return 'hello world'\n")
    _seed_text(inbox, "notes.txt", "Meeting notes:\n- item 1\n- item 2\n")

    call_count = [0]

    def mock_run(src, wiki_dir, max_cycles, source_id):
        call_count[0] += 1
        assert call_count[0] <= 5, "spin guard: too many run_inner calls"
        return _fake_result(True)

    with patch("wiki_weaver.engine_runner.run_inner", side_effect=mock_run):
        rc = cmd_ingest(_args(wiki))

    assert rc == 0, f"all text sources converged — exit code must be 0, got {rc}"
    assert call_count[0] == 2, (
        f"expected run_inner called 2 times (foo.py, notes.txt); got {call_count[0]}"
    )

    # Both files must be in _archive/.
    assert (archive / "foo.py").exists(), "foo.py must be in _archive/"
    assert (archive / "notes.txt").exists(), "notes.txt must be in _archive/"

    # Inbox must be empty (no non-hidden files).
    remaining = [
        p for p in inbox.iterdir() if p.is_file() and not p.name.startswith(".")
    ]
    assert not remaining, f"inbox must be empty; found: {[p.name for p in remaining]}"


# ---------------------------------------------------------------------------
# Test B — binary file routes to _failed/; run_inner NOT called; rc nonzero
# ---------------------------------------------------------------------------


def test_b_binary_routes_to_failed_no_run_inner(tmp_path: Path) -> None:
    """A binary file (NUL byte) must be routed to _failed/ without calling run_inner.

    Scenario:
      - _inbox/ contains only blob.png (bytes include NUL → binary).
      - _looks_like_text() returns False → pre-empted before run_inner.
      - blob.png lands in _failed/ (collision-safe move).
      - run_inner is never called.
      - Inbox is empty (loop terminates, no spin).
      - Exit code is nonzero (binary failure counted in failed_n).
    """
    wiki = _make_wiki(tmp_path)
    inbox = wiki / INBOX
    failed_dir = wiki / FAILED

    _seed_binary(inbox, "blob.png")

    with patch("wiki_weaver.engine_runner.run_inner") as mock_run:
        rc = cmd_ingest(_args(wiki))

    # run_inner must NOT be called for a binary source.
    mock_run.assert_not_called()

    # Exit code must be nonzero (binary = failure).
    assert rc != 0, "binary source → exit code must be nonzero"

    # _failed/ must exist and contain the binary file.
    assert failed_dir.exists(), "_failed/ directory must be created"
    failed_files = list(failed_dir.glob("blob*"))
    assert failed_files, (
        f"_failed/ must contain blob.png; found: {list(failed_dir.iterdir())}"
    )

    # Inbox must be empty (file was moved to _failed/, loop terminated).
    remaining = [
        p for p in inbox.iterdir() if p.is_file() and not p.name.startswith(".")
    ]
    assert not remaining, (
        f"inbox must be empty after binary drain; found: {[p.name for p in remaining]}"
    )


# ---------------------------------------------------------------------------
# Test C — .md regression + mixed inbox (.md, .rs, binary, .DS_Store)
# ---------------------------------------------------------------------------


def test_c_mixed_inbox_md_rs_binary_ds_store(tmp_path: Path) -> None:
    """Mixed inbox: .md and .rs go to _archive, binary to _failed, .DS_Store skipped.

    Scenario:
      - _inbox/ contains: a.md, b.rs, evil.bin (binary), .DS_Store (hidden — must be skipped).
      - a.md (UTF-8 markdown) → _archive/
      - b.rs  (UTF-8 Rust)    → _archive/
      - evil.bin (binary NUL) → _failed/
      - .DS_Store (hidden)    → skipped entirely (stays in inbox, never touched)
      - run_inner called exactly twice (a.md, b.rs).
      - Exit code nonzero (binary failure).
    """
    wiki = _make_wiki(tmp_path)
    inbox = wiki / INBOX
    archive = wiki / ARCHIVE
    failed_dir = wiki / FAILED

    _seed_text(inbox, "a.md", "# Article\n\nBody text about something interesting.\n")
    _seed_text(inbox, "b.rs", 'fn main() {\n    println!("hello");\n}\n')
    _seed_binary(inbox, "evil.bin")
    # .DS_Store: hidden file (starts with .) — must be silently skipped
    ds = inbox / ".DS_Store"
    ds.write_bytes(b"\x00\x00\x00\x01\x42\x44\x00\x00")  # fake DS_Store bytes
    old = time.time() - 10
    os.utime(ds, (old, old))

    call_count = [0]
    ingested_names: list[str] = []

    def mock_run(src, wiki_dir, max_cycles, source_id):
        call_count[0] += 1
        assert call_count[0] <= 5, "spin guard: too many run_inner calls"
        ingested_names.append(src.name)
        return _fake_result(True)

    with patch("wiki_weaver.engine_runner.run_inner", side_effect=mock_run):
        rc = cmd_ingest(_args(wiki))

    # run_inner called exactly twice (a.md and b.rs, NOT evil.bin, NOT .DS_Store).
    assert call_count[0] == 2, (
        f"expected run_inner called 2 times; got {call_count[0]} (for: {ingested_names})"
    )
    assert "a.md" in ingested_names, "a.md must have been passed to run_inner"
    assert "b.rs" in ingested_names, "b.rs must have been passed to run_inner"

    # .md and .rs land in _archive/.
    assert (archive / "a.md").exists(), "a.md must be in _archive/"
    assert (archive / "b.rs").exists(), "b.rs must be in _archive/"

    # Binary lands in _failed/.
    assert failed_dir.exists(), "_failed/ directory must be created"
    failed_files = list(failed_dir.glob("evil*"))
    assert failed_files, (
        f"_failed/ must contain evil.bin; found: {list(failed_dir.iterdir())}"
    )

    # .DS_Store must NOT have been moved — it should remain in inbox.
    assert (inbox / ".DS_Store").exists(), (
        ".DS_Store must remain in inbox (skipped, never moved)"
    )

    # No non-hidden ingestable files remain in inbox.
    remaining = [
        p for p in inbox.iterdir() if p.is_file() and not p.name.startswith(".")
    ]
    assert not remaining, (
        f"no non-hidden files should remain in inbox; found: {[p.name for p in remaining]}"
    )

    # Exit code nonzero: one binary failure.
    assert rc != 0, "binary failure → exit code must be nonzero"
