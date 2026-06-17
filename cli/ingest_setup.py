# pyright: reportMissingImports=false
"""Setup tool for the ingest.dot pipeline.

Picks the next ready source from the inbox, assigns a stable source id
(reusing cli/lib.py's _assign_source_id), and prints ONE JSON object to
stdout. The parse_json="true" attribute on the calling tool node in
ingest.dot consumes this output: it runs the command, parses stdout as
JSON, and deposits each key into the engine's shared context so the
child synthesize.dot pipeline can reference them as $source_path, etc.

Usage:
    python <this_file> <inbox_or_wiki_dir> <wiki_dir>

    inbox_or_wiki_dir  -- if it contains an _inbox/ subdirectory, the
                          actual inbox is <inbox_or_wiki_dir>/_inbox.
                          Otherwise the directory is used as the inbox
                          directly.
    wiki_dir           -- the wiki root (registry, policy, etc.)

Exits non-zero with a message on stderr when no ready source is found.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# When executed as a standalone script (via tool_command in ingest.dot), Python
# adds only the script's directory (wiki-weaver/cli/) to sys.path, not the repo
# root.  Add the repo root explicitly so that `from cli.* import ...` works.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


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
        print(f"ERROR: inbox not found: {inbox}", file=sys.stderr)
        return 1

    # Import the library functions and engine constants lazily so
    # init/lint/doctor do not pay the cost of loading them.
    from cli.engine_runner import FOOTNOTES_PY, NORMALIZE_PY, VALIDATE_PY
    from cli.lib import _assign_source_id, _looks_like_text, _processed_sources
    from cli.policy import load_policy

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
        print("ERROR: no ready source found in inbox", file=sys.stderr)
        return 1

    # Build the JSON context block that synthesize.dot needs.
    # Keys mirror the $var substitutions that build_dot() makes in the
    # direct-path case, so synthesize.dot sees identical context regardless
    # of whether it is invoked directly or as a folder sub-pipeline.
    validation_report = wiki_dir / ".ai" / "validation.md"
    validate_cmd = (
        f"{sys.executable} {VALIDATE_PY} {wiki_dir} --out {validation_report}"
    )
    if policy.validator_config_path is not None:
        validate_cmd += f" --config {policy.validator_config_path}"
    normalize_cmd = f"{sys.executable} {NORMALIZE_PY} {wiki_dir}"
    footnotes_cmd = f"{sys.executable} {FOOTNOTES_PY} {wiki_dir}"

    result = {
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
    }
    # Single JSON object, exactly as parse_json="true" expects.
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
