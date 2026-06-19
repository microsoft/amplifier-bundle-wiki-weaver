# pyright: reportMissingImports=false
"""Setup tool for the ingest.dot pipeline.

Picks the next ready source from the inbox, assigns a stable source id
(reusing cli/lib.py's _assign_source_id), and prints ONE JSON object to
stdout. The parse_json="true" attribute on the calling tool node in
ingest.dot consumes this output: it runs the command, parses stdout as
JSON, and deposits each key into the engine's shared context so the
child synthesize.dot pipeline can reference them as $source_path, etc.

When no ready source is found (inbox empty OR all files still within the
2-second debounce window), exits 0 and prints {"has_source":"false"}.
This is the NORMAL drain-complete signal, NOT an error.

Usage:
    python <this_file> <inbox_or_wiki_dir> <wiki_dir>

    inbox_or_wiki_dir  -- if it contains an _inbox/ subdirectory, the
                          actual inbox is <inbox_or_wiki_dir>/_inbox.
                          Otherwise the directory is used as the inbox
                          directly.
    wiki_dir           -- the wiki root (registry, policy, etc.)

Exits non-zero ONLY on hard errors (bad args, missing wiki_dir).
Inbox empty or debounce → exit 0 with {"has_source":"false"}.
"""

from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path

# When executed as a standalone script (via tool_command in ingest.dot), Python
# adds only the script's directory (wiki-weaver/cli/) to sys.path, not the repo
# root.  Add the repo root explicitly so that `from wiki_weaver.* import ...` works.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Absolute paths to the archive, fail, and tamper-check CLI scripts (sibling scripts in cli/).
_CLI_DIR = Path(__file__).resolve().parent
INGEST_ARCHIVE_PY = _CLI_DIR / "ingest_archive.py"
INGEST_FAIL_PY = _CLI_DIR / "ingest_fail.py"
INGEST_TAMPER_CHECK_PY = _CLI_DIR / "ingest_tamper_check.py"


def main() -> int:
    if len(sys.argv) < 3:
        print(
            f"usage: {sys.argv[0]} <inbox_or_wiki_dir> <wiki_dir>",
            file=sys.stderr,
        )
        return 1

    inbox_or_wiki = Path(sys.argv[1]).resolve()
    wiki_dir = Path(sys.argv[2]).resolve()

    if not wiki_dir.is_dir():
        print(f"ERROR: wiki_dir not found: {wiki_dir}", file=sys.stderr)
        return 1

    # Determine actual inbox directory.
    # If the first arg has an _inbox subdirectory, use that.
    # Otherwise use the first arg directly (allows an explicit inbox path).
    candidate_inbox = inbox_or_wiki / "_inbox"
    inbox = candidate_inbox if candidate_inbox.is_dir() else inbox_or_wiki

    if not inbox.is_dir():
        # No inbox at all → treat as drain-complete (not a hard error).
        print(json.dumps({"has_source": "false"}))
        return 0

    # Import the library functions and engine constants lazily so
    # init/lint/doctor do not pay the cost of loading them.
    from wiki_weaver.engine_runner import FOOTNOTES_PY, NORMALIZE_PY, VALIDATE_PY
    from wiki_weaver.lib import (
        _assign_source_id,
        _looks_like_text,
        _processed_sources,
        _snapshot_process_state,
    )
    from wiki_weaver.policy import load_policy

    policy = load_policy(wiki_dir)
    processed = _processed_sources(wiki_dir)

    # Find the next ready, non-binary, not-yet-ingested source.
    # Debounce: skip files written < 2 s ago (half-written by a concurrent
    # producer) -- same logic as the drain loop in cli/lib.py.
    _DEBOUNCE_SECS = 2.0
    pending = sorted(
        p for p in inbox.iterdir() if p.is_file() and not p.name.startswith(".")
    )
    now = time.time()
    ready = [p for p in pending if (now - p.stat().st_mtime) >= _DEBOUNCE_SECS]

    src: Path | None = None
    source_id: int | str = ""
    for candidate in ready:
        if not _looks_like_text(candidate):
            continue
        entry, _ = _assign_source_id(wiki_dir, candidate)
        if entry.get("ingested") or candidate.name in processed:
            continue
        src = candidate
        source_id = entry["id"]
        break

    if src is None:
        # Inbox empty or all sources already ingested → drain complete.
        print(json.dumps({"has_source": "false"}))
        return 0

    # Build the JSON context block that synthesize.dot needs.
    # Keys mirror the $var substitutions that build_dot() makes in the
    # direct-path case, so synthesize.dot sees identical context regardless
    # of whether it is invoked directly or as a folder sub-pipeline.
    validation_report = wiki_dir / ".ai" / "validation.md"
    validate_cmd = (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(VALIDATE_PY))}"
        f" {shlex.quote(str(wiki_dir))} --out {shlex.quote(str(validation_report))}"
    )
    if policy.validator_config_path is not None:
        validate_cmd += f" --config {shlex.quote(str(policy.validator_config_path))}"
    normalize_cmd = (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(NORMALIZE_PY))}"
        f" {shlex.quote(str(wiki_dir))}"
    )
    footnotes_cmd = (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(FOOTNOTES_PY))}"
        f" {shlex.quote(str(wiki_dir))}"
    )

    # Fully-formed archive and fail commands, constructed with sys.executable
    # so they run in the same interpreter that launched this script.
    # All paths are shell-quoted so filenames with spaces or special characters
    # are passed as a single argument to each script.  The tool_command handler
    # in the attractor engine uses asyncio.create_subprocess_shell, which passes
    # the string to /bin/sh -c; without quoting, a filename like
    # "Call with Brian.md" would be word-split by the shell into three separate
    # argv entries, breaking archive/fail operations for any source with spaces.
    archive_cmd = (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(INGEST_ARCHIVE_PY))}"
        f" {shlex.quote(str(wiki_dir))} {shlex.quote(str(src))} {shlex.quote(str(source_id))}"
    )
    fail_cmd = (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(INGEST_FAIL_PY))}"
        f" {shlex.quote(str(wiki_dir))} {shlex.quote(str(src))}"
    )

    # Snapshot process state BEFORE synthesis so the tamper-check node can
    # detect any ledger lines or archive moves the LLM performed during synthesis
    # (which are CLI-exclusive operations).  The snapshot is serialised to a
    # per-source JSON file and its path is embedded in tamper_check_cmd so the
    # tamper_check node can read it without any additional context keys.
    ledger_lines_before, archive_files_before = _snapshot_process_state(wiki_dir)
    snapshot_path = wiki_dir / ".ai" / f".tamper_snapshot_{source_id}.json"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(
        json.dumps(
            {
                "ledger_lines": ledger_lines_before,
                "archive_files": sorted(archive_files_before),
            }
        ),
        encoding="utf-8",
    )
    tamper_check_cmd = (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(INGEST_TAMPER_CHECK_PY))}"
        f" {shlex.quote(str(wiki_dir))} {shlex.quote(str(snapshot_path))}"
    )

    result = {
        "has_source": "true",
        "source_path": str(src),
        "source_id": str(source_id),
        "wiki_dir": str(wiki_dir),
        "validation_report": str(validation_report),
        "schema_path": str(policy.schema_path),
        "convergence_rubric": str(policy.convergence_rubric_path),
        "max_cycles": str(policy.max_cycles),
        "normalize_cmd": normalize_cmd,
        "footnotes_cmd": footnotes_cmd,
        "validate_cmd": validate_cmd,
        "archive_cmd": archive_cmd,
        "fail_cmd": fail_cmd,
        "tamper_check_cmd": tamper_check_cmd,
    }
    # Single JSON object, exactly as parse_json="true" expects.
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
