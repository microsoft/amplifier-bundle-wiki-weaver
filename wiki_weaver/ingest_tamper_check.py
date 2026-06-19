# pyright: reportMissingImports=false
"""Tamper-check tool for the ingest.dot drain loop.

Reads a before-synthesis state snapshot (written by ingest_setup.py), runs
_detect_and_undo_tamper from cli/lib.py, cleans up the snapshot file, and
emits ONE JSON object to stdout.

The tamper guard enforces the CLI-exclusive invariant: the ledger
(.processed.jsonl) and the _archive/ directory are written ONLY by the CLI
(ingest_archive.py), NEVER by the LLM during synthesis.  If the LLM wrote
either during synthesize.dot, _detect_and_undo_tamper undoes the damage and
we fail loud by emitting {"tampered": "true"} (which the ingest.dot router
sends to fail_handler, routing the source to _failed/).

On clean: emits {"tampered": "false"}, exit 0.
On tamper: undoes fabricated records, prints error to stderr,
           emits {"tampered": "true"}, exit 0.
           (Exit 0 so the engine reads the JSON and routes via context.)

Usage:
    python <this_file> <wiki_dir> <snapshot_path>

    wiki_dir       -- the wiki root (contains _archive/, .processed.jsonl, etc.)
    snapshot_path  -- path to the before-snapshot JSON written by ingest_setup.py
                      (format: {"ledger_lines": int, "archive_files": [str, ...]})

Exits non-zero ONLY on hard errors (bad args, missing wiki_dir, unreadable snapshot).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure the repo root is on sys.path so `from wiki_weaver.* import` works when
# this script is invoked directly (e.g. via tool_command in ingest.dot).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def main() -> int:
    if len(sys.argv) < 3:
        print(
            f"usage: {sys.argv[0]} <wiki_dir> <snapshot_path>",
            file=sys.stderr,
        )
        return 1

    wiki_dir = Path(sys.argv[1]).resolve()
    snapshot_path = Path(sys.argv[2]).resolve()

    if not wiki_dir.is_dir():
        print(f"ERROR: wiki_dir not found: {wiki_dir}", file=sys.stderr)
        return 1

    if not snapshot_path.is_file():
        print(f"ERROR: snapshot not found: {snapshot_path}", file=sys.stderr)
        return 1

    # Read the before-synthesis snapshot.
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        ledger_lines = int(snapshot["ledger_lines"])
        archive_files: set[str] = set(snapshot["archive_files"])
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: malformed snapshot: {exc}", file=sys.stderr)
        # Clean up snapshot file even on read errors.
        try:
            snapshot_path.unlink(missing_ok=True)
        except OSError:
            pass
        return 1

    # Clean up snapshot file regardless of tamper outcome.
    try:
        snapshot_path.unlink(missing_ok=True)
    except OSError:
        pass

    from wiki_weaver.lib import _detect_and_undo_tamper

    before: tuple[int, set[str]] = (ledger_lines, archive_files)
    violations = _detect_and_undo_tamper(wiki_dir, before)

    if violations:
        print(
            "TAMPER DETECTED -- the ingest agent wrote process state it does not own. "
            "Fabricated records were reverted. Source routed to _failed/.",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        # Exit 0 so the engine reads our JSON output and routes via context.tampered=true.
        print(json.dumps({"tampered": "true"}))
        return 0

    print(json.dumps({"tampered": "false"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
