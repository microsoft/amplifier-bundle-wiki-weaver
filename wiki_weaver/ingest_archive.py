# pyright: reportMissingImports=false
"""Archive tool for the ingest.dot drain loop.

Called by the `archive` tool node in ingest.dot after synthesize.dot
reports convergence (outcome=success). Performs the three deterministic
process-state writes that are the CLI's exclusive job:

  1. Move source from _inbox/ to _archive/ (collision-safe).
  2. Append a ledger entry to .processed.jsonl.
  3. Mark the source as ingested in .sources.json.

Reuses the exact functions from cli/lib.py that the Python drain loop
in cli/lib.py:ingest() uses -- no reimplementation.

Usage:
    python <this_file> <wiki_dir> <source_path> <source_id>

    wiki_dir     -- the wiki root (contains _archive/, .processed.jsonl, etc.)
    source_path  -- absolute path to the source file in _inbox/
    source_id    -- stable integer id previously assigned by ingest_setup.py

Exits 0 on success.  Exits non-zero on hard errors (bad args, missing wiki,
source file not found, IO failure).
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

# Ensure the repo root is on sys.path so `from wiki_weaver.* import` works when
# this script is invoked directly (e.g. via tool_command in ingest.dot).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _find_ingest_logs_dir(wiki_dir: Path) -> Path:
    """Return the most-recent ingest-* run directory under <wiki>/.runs/.

    run_ingest() (engine_runner.py) creates ``<wiki>/.runs/ingest-YYYYMMDD-HHMMSS/``
    and writes ``ingest.dot`` there *before* launching the engine, so this directory
    always exists by the time any tool node (including archive) fires.

    Raises RuntimeError loudly if no ingest-* directory is found -- ingest_archive
    must only be called from within an active run_ingest() pipeline.
    """
    runs_dir = wiki_dir / ".runs"
    if not runs_dir.is_dir():
        raise RuntimeError(
            f"no .runs/ directory found under {wiki_dir}; "
            "ingest_archive must be called from within a run_ingest() pipeline"
        )
    candidates = sorted(
        p for p in runs_dir.iterdir() if p.is_dir() and p.name.startswith("ingest-")
    )
    if not candidates:
        raise RuntimeError(
            f"no ingest-* run directory found under {runs_dir}; "
            "ingest_archive must be called from within a run_ingest() pipeline"
        )
    # Dir names are ingest-YYYYMMDD-HHMMSS; lexicographic order == chronological.
    return candidates[-1]


def main() -> int:
    if len(sys.argv) < 4:
        print(
            f"usage: {sys.argv[0]} <wiki_dir> <source_path> <source_id>",
            file=sys.stderr,
        )
        return 1

    wiki_dir = Path(sys.argv[1]).resolve()
    source_path = Path(sys.argv[2]).resolve()
    source_id_raw = sys.argv[3]

    if not wiki_dir.is_dir():
        print(f"ERROR: wiki_dir not found: {wiki_dir}", file=sys.stderr)
        return 1

    # Determine the run-logs directory for this drain run.  run_ingest() creates
    # <wiki>/.runs/ingest-YYYYMMDD-HHMMSS/ before launching the engine; that dir
    # is the honest logs_dir for every source archived during this pipeline run.
    # Fail loud if it doesn't exist -- this script must not run outside a drain.
    try:
        run_logs_dir = _find_ingest_logs_dir(wiki_dir)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    try:
        source_id = int(source_id_raw)
    except ValueError:
        print(
            f"ERROR: source_id must be an integer, got: {source_id_raw!r}",
            file=sys.stderr,
        )
        return 1

    from wiki_weaver.lib import (
        ARCHIVE,
        _append_ledger,
        _collision_safe_move,
        _mark_source_ingested,
        _source_hash,
    )

    archive_dir = wiki_dir / ARCHIVE
    archive_dir.mkdir(exist_ok=True)

    # Hash BEFORE moving so we can record it in the ledger even if the
    # source was already moved (graceful idempotence on retry).
    if source_path.is_file():
        file_hash = _source_hash(source_path)
    else:
        # Source already moved (e.g. a retry after a partial run).
        # Attempt to find it in _archive/ to retrieve its hash.
        candidate = archive_dir / source_path.name
        if candidate.is_file():
            file_hash = _source_hash(candidate)
            print(
                f"NOTE: source already in _archive/ ({candidate.name}); "
                "ledger + registry update only.",
                file=sys.stderr,
            )
            dest = candidate
            _append_ledger(
                wiki_dir,
                {
                    "source": source_path.name,
                    "source_id": source_id,
                    "hash": file_hash,
                    "status": "success",
                    "converged": True,
                    "archived_to": str(dest),
                    "logs_dir": str(run_logs_dir),
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                },
            )
            _mark_source_ingested(wiki_dir, file_hash)
            print(
                f"archived (already present): {source_path.name} -> {dest}",
                file=sys.stderr,
            )
            return 0
        else:
            print(
                f"ERROR: source not found in inbox or archive: {source_path}",
                file=sys.stderr,
            )
            return 1

    # Move source from _inbox/ to _archive/ (collision-safe rename).
    dest = _collision_safe_move(source_path, archive_dir)

    # Append ledger entry (same schema as the Python drain loop in lib.py).
    _append_ledger(
        wiki_dir,
        {
            "source": source_path.name,
            "source_id": source_id,
            "hash": file_hash,
            "status": "success",
            "converged": True,
            "archived_to": str(dest),
            "logs_dir": str(run_logs_dir),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        },
    )

    # Mark ingested in .sources.json registry.
    _mark_source_ingested(wiki_dir, file_hash)

    print(
        f"archived: {source_path.name} -> {dest.name}  "
        f"(id={source_id} hash={file_hash[:12]})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
