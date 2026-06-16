# pyright: reportMissingImports=false
"""wiki-weaver library API.

Importable concept-level functions that back the CLI and can be called
directly by other Python code (tests, the future attractor shim, etc.).
The CLI (wiki_weaver.py) is a thin argparse wrapper around these.

Public API
----------
init(wiki_dir)                      scaffold a fresh wiki
ingest(wiki, *, source, ...)        integrate inbox sources via the engine
lint(wiki)                          run the structural validator
doctor(*, wiki)                     environment diagnostics
query(wiki, term)                   list pages matching a term (stub)
ask(wiki, question, *, json_out)    answer a question from the compiled wiki
rag(articles, question, *, json_out) naive-RAG baseline from raw sources

All functions print their own output (unchanged from the original cmd_*
behaviour) and return an integer exit code (0 = success).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"

LEDGER_NAME = ".processed.jsonl"
INBOX = "_inbox"
ARCHIVE = "_archive"
FAILED = "_failed"

# Pipeline assets live alongside the cli/ package's repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATE_PY = REPO_ROOT / "pipeline" / "validate_wiki.py"


def _ok(msg: str) -> None:
    print(f"{GREEN}\u2713{RESET} {msg}")


def _fail(msg: str) -> None:
    print(f"{RED}\u2717{RESET} {msg}")


def _warn(msg: str) -> None:
    print(f"{YELLOW}!{RESET} {msg}")


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

INDEX_TEMPLATE = """\
---
title: Index
type: index
sources: []
last_updated: {today}
---

# Index

Catalog of wiki pages, grouped by type. (Maintained by the ingest pipeline.)
"""

OVERVIEW_TEMPLATE = """\
---
title: Overview
type: overview
sources: []
last_updated: {today}
---

# Overview

One-paragraph orientation to this wiki. (Maintained by the ingest pipeline.)
"""


def init(wiki_dir: str | Path) -> int:
    """Scaffold a fresh wiki directory."""
    wiki = Path(wiki_dir).resolve()
    (wiki / INBOX).mkdir(parents=True, exist_ok=True)
    (wiki / ARCHIVE).mkdir(parents=True, exist_ok=True)
    (wiki / ".ai" / "feedback").mkdir(parents=True, exist_ok=True)

    today = date.today().isoformat()
    index = wiki / "index.md"
    overview = wiki / "overview.md"
    if not index.exists():
        index.write_text(INDEX_TEMPLATE.format(today=today), encoding="utf-8")
    if not overview.exists():
        overview.write_text(OVERVIEW_TEMPLATE.format(today=today), encoding="utf-8")

    ledger = wiki / LEDGER_NAME
    if not ledger.exists():
        ledger.touch()

    _ok(f"initialized wiki at {wiki}")
    print(
        f"  {INBOX}/  {ARCHIVE}/  .ai/feedback/  index.md  overview.md  {LEDGER_NAME}"
    )
    return 0


# ---------------------------------------------------------------------------
# ledger helpers
# ---------------------------------------------------------------------------


def _read_ledger(wiki: Path) -> list[dict]:
    ledger = wiki / LEDGER_NAME
    if not ledger.exists():
        return []
    rows: list[dict] = []
    for line in ledger.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _processed_sources(wiki: Path) -> set[str]:
    return {row.get("source", "") for row in _read_ledger(wiki)}


def _append_ledger(wiki: Path, entry: dict) -> None:
    with (wiki / LEDGER_NAME).open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Fix 3 -- persistent source registry (stable ids + content-hash dedupe)
# ---------------------------------------------------------------------------
#
# Source ids used to be guessed per-run by the ingest LLM ([1]/[2]/[3]), which
# collided across runs and produced duplicate summary pages on re-ingest. The
# registry at <wiki>/.sources.json is the single source of truth: the CLI
# assigns/looks up a stable id by CONTENT HASH *before* ingest and threads it
# into the inner pipeline as $source_id. An already-ingested source (same hash)
# is deduped and skipped.

REGISTRY_NAME = ".sources.json"


def _parse_transcript_header(text: str) -> dict:
    """Parse provenance from a meeting-transcript header block (no YAML frontmatter).

    Handles the format produced by Teams/Zoom/calendar export tools::

        # Transcript: Team Pulse Weekly Planning

        Source: https://microsoft-my.sharepoint.com/...
        Duration: 1:00:50
        Speakers: Chris Park, Alex Rivera, Samuel Lee
        Date: 5/29/2026, 11:07:43 AM
        Chat type: Meeting
        Attendees: Samuel Lee, Chris Park, Alex Rivera

        ---

        [0:00:04] Chris Park: ...

    Returns a dict with keys ``author``, ``url``, ``date``, ``title`` (all
    default to ``None``). Returns all-None for files that are not recognised
    as transcripts — graceful fallback, no crash, no fabrication.

    Detection: at least one of ``Speakers:`` or ``Attendees:`` must appear in
    the header block (lines before the first ``---`` separator, or the first
    50 lines when no separator is present). Labelled-field matching is
    case-insensitive. ``Source:`` is only accepted when the value starts with
    ``http://`` or ``https://`` to avoid false positives on prose fragments.
    """
    result: dict = {"author": None, "url": None, "date": None, "title": None}
    lines = text.splitlines()

    # Locate the header block: up to the first "---" separator (the thematic
    # break that ends the metadata preamble) or a 50-line cap.
    sep_idx: int | None = None
    for i, line in enumerate(lines):
        if line.strip() == "---":
            sep_idx = i
            break
    header_lines = lines[: sep_idx if sep_idx is not None else min(50, len(lines))]

    # Labelled-field regex: "Label Name: value" (multi-word keys allowed)
    _labeled = re.compile(r"^([A-Za-z][A-Za-z0-9 ]*?):\s*(.+)$")

    has_speaker_marker = False  # True when Speakers: or Attendees: found
    attendees_val: str | None = None

    for line in header_lines:
        stripped = line.strip()

        # Extract title from the first markdown heading
        if stripped.startswith("#") and result["title"] is None:
            heading = re.sub(r"^#+\s*", "", stripped)
            heading = re.sub(r"(?i)^Transcript:\s*", "", heading).strip()
            if heading:
                result["title"] = heading
            continue

        m = _labeled.match(stripped)
        if not m:
            continue
        key = m.group(1).strip().lower()
        val = m.group(2).strip()
        if not val:
            continue

        if key == "source":
            # Accept only URLs to avoid false positives on prose like "Source: Smith 2024"
            if val.startswith(("http://", "https://")):
                result["url"] = val
        elif key == "speakers":
            result["author"] = val
            has_speaker_marker = True
        elif key == "date":
            result["date"] = val
        elif key == "attendees":
            attendees_val = val
            has_speaker_marker = True

    # Speakers: takes priority; Attendees: is the fallback author
    if result["author"] is None and attendees_val is not None:
        result["author"] = attendees_val

    # If no speaker/attendee marker found this is not a transcript — return all-None
    if not has_speaker_marker:
        return {"author": None, "url": None, "date": None, "title": None}

    return result


def _read_source_frontmatter(src: Path) -> dict:
    """Extract author, url, and date from YAML frontmatter of a source article.

    Reads the ``---`` … ``---`` frontmatter block and returns a dict with keys
    ``author``, ``url``, and ``date`` (all defaulting to ``None`` when absent).

    Handles simple single-line string fields only (quoted or unquoted). Fails
    silently on any parse error — provenance is best-effort; missing fields are
    stored as ``None`` in the registry, never as fabrications.

    Recognises both ``source:`` and ``url:`` as the URL field (medium-tools
    writes ``source:``, other producers may write ``url:``).

    Fallback: when no YAML frontmatter is found, attempts to parse a
    meeting-transcript header block via :func:`_parse_transcript_header`.
    Sources with neither frontmatter nor transcript markers are unchanged
    (returns all-None). The medium-article path is byte-identical.
    """
    result: dict = {"author": None, "url": None, "date": None}
    try:
        text = src.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        # No YAML frontmatter — try transcript header as graceful fallback.
        fm = _parse_transcript_header(text)
        if fm.get("author"):
            result["author"] = fm["author"]
        if fm.get("url"):
            result["url"] = fm["url"]
        if fm.get("date"):
            result["date"] = fm["date"]
        return result

    end_idx = None
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return result

    # Regex for `key: "quoted value"` or `key: 'quoted value'` or `key: plain value`
    _quoted = re.compile(r'^(\w+):\s*["\'](.*)["\']$')
    _plain = re.compile(r"^(\w+):\s*(.+)$")

    for line in lines[1:end_idx]:
        line = line.strip()
        m = _quoted.match(line) or _plain.match(line)
        if not m:
            continue
        key = m.group(1).lower()
        val = m.group(2).strip().strip("\"'")
        if not val:
            continue
        if key == "author":
            result["author"] = val
        elif key in ("source", "url"):
            result["url"] = val
        elif key == "date":
            result["date"] = val

    return result


def _source_hash(src: Path) -> str:
    """Stable content hash (sha256) of a source file."""
    h = hashlib.sha256()
    h.update(src.read_bytes())
    return h.hexdigest()


def _load_registry(wiki: Path) -> dict:
    reg_path = wiki / REGISTRY_NAME
    if not reg_path.exists():
        return {"version": 1, "next_id": 1, "sources": []}
    try:
        data = json.loads(reg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "next_id": 1, "sources": []}
    if not isinstance(data, dict):
        return {"version": 1, "next_id": 1, "sources": []}
    data.setdefault("version", 1)
    data.setdefault("next_id", 1)
    data.setdefault("sources", [])
    return data


def _save_registry(wiki: Path, registry: dict) -> None:
    """Atomic write of the registry (tmp + replace)."""
    reg_path = wiki / REGISTRY_NAME
    tmp = reg_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")
    tmp.replace(reg_path)


def _registry_entry_for_hash(registry: dict, file_hash: str) -> dict | None:
    for entry in registry.get("sources", []):
        if entry.get("hash") == file_hash:
            return entry
    return None


def _assign_source_id(wiki: Path, src: Path) -> tuple[dict, bool]:
    """Look up or assign a stable id for ``src`` by content hash.

    Returns ``(entry, is_new)``. ``entry`` always has id/filename/hash/ingested.
    On a new source the registry is persisted immediately so the id is stable
    even if the ingest run later fails or is retried.

    Provenance fields (author, url, date) are read from the source file's YAML
    frontmatter and stored alongside id/filename/hash so citation ``[N]`` can
    resolve to a real author + URL.  Missing fields are stored as ``None``
    (or omitted) — never fabricated.
    """
    file_hash = _source_hash(src)
    registry = _load_registry(wiki)
    existing = _registry_entry_for_hash(registry, file_hash)
    if existing is not None:
        return existing, False

    fm = _read_source_frontmatter(src)
    entry: dict = {
        "id": int(registry["next_id"]),
        "filename": src.name,
        "hash": file_hash,
        "first_seen": datetime.now().isoformat(timespec="seconds"),
        "ingested": False,
    }
    # Provenance from frontmatter — store only fields that are present so the
    # registry stays clean (no null-value noise for articles without metadata).
    if fm.get("author"):
        entry["author"] = fm["author"]
    if fm.get("url"):
        entry["url"] = fm["url"]
    if fm.get("date"):
        entry["date"] = fm["date"]

    registry["sources"].append(entry)
    registry["next_id"] = int(registry["next_id"]) + 1
    _save_registry(wiki, registry)
    return entry, True


def _mark_source_ingested(wiki: Path, file_hash: str) -> None:
    registry = _load_registry(wiki)
    entry = _registry_entry_for_hash(registry, file_hash)
    if entry is not None and not entry.get("ingested"):
        entry["ingested"] = True
        entry["ingested_at"] = datetime.now().isoformat(timespec="seconds")
        _save_registry(wiki, registry)


# ---------------------------------------------------------------------------
# Fix 1b -- deterministic tamper guard (the safety net under fs sandboxing)
# ---------------------------------------------------------------------------
#
# Process state (the ledger + _archive/) is the CLI's EXCLUSIVE job and is
# written ONLY here, AFTER a real convergence. The spawned ingest node is
# additionally sandboxed at the filesystem-tool layer (engine_runner Fix 1),
# but tool-bash has no path sandbox, so we ALSO verify deterministically: snap
# the ledger + archive before the inner run; if EITHER changed during the run,
# the agent fabricated process state. We never trust it -- we restore the
# pre-run state (drop fabricated ledger lines, return falsely-archived files to
# the inbox) and FAIL LOUD.


def _snapshot_process_state(wiki: Path) -> tuple[int, set[str]]:
    ledger = wiki / LEDGER_NAME
    ledger_lines = (
        len(ledger.read_text(encoding="utf-8").splitlines()) if ledger.exists() else 0
    )
    archive = wiki / ARCHIVE
    archive_files = {p.name for p in archive.iterdir()} if archive.is_dir() else set()
    return ledger_lines, archive_files


def _detect_and_undo_tamper(wiki: Path, before: tuple[int, set[str]]) -> list[str]:
    """Compare process state to the pre-run snapshot; undo + report tamper.

    Returns a list of human-readable violation strings (empty == clean).
    """
    before_lines, before_archive = before
    violations: list[str] = []

    # (1) Ledger: any new line during the inner run is agent-fabricated, since
    # the lib appends only after this guard runs. Truncate back to before_lines.
    ledger = wiki / LEDGER_NAME
    if ledger.exists():
        lines = ledger.read_text(encoding="utf-8").splitlines()
        if len(lines) > before_lines:
            fabricated = lines[before_lines:]
            violations.append(
                f"agent wrote {len(fabricated)} fabricated ledger line(s): "
                + "; ".join(s[:160] for s in fabricated)
            )
            kept = lines[:before_lines]
            ledger.write_text(
                ("\n".join(kept) + "\n") if kept else "", encoding="utf-8"
            )

    # (2) Archive: any file that appeared during the inner run is an
    # agent-performed move. Return it to the inbox so it is NOT falsely treated
    # as processed, and so the source can be re-ingested honestly.
    archive = wiki / ARCHIVE
    inbox = wiki / INBOX
    if archive.is_dir():
        now_archive = {p.name for p in archive.iterdir()}
        new_files = sorted(now_archive - before_archive)
        if new_files:
            violations.append(
                "agent moved source(s) into _archive/ (CLI-exclusive): "
                + ", ".join(new_files)
            )
            inbox.mkdir(exist_ok=True)
            for name in new_files:
                try:
                    (archive / name).replace(inbox / name)
                except OSError:
                    pass

    return violations


# ---------------------------------------------------------------------------
# ingest helpers
# ---------------------------------------------------------------------------


def _collision_safe_move(src: Path, dest_dir: Path) -> Path:
    """Move *src* into *dest_dir*, adding an integer suffix if the name is taken.

    Returns the final destination path.  Raises RuntimeError only on extreme
    collision counts (>= 10,000), which should never occur in practice.
    """
    dest = dest_dir / src.name
    if not dest.exists():
        src.replace(dest)
        return dest
    stem, suffix = src.stem, src.suffix
    for i in range(1, 10_000):
        candidate = dest_dir / f"{stem}.{i}{suffix}"
        if not candidate.exists():
            src.replace(candidate)
            return candidate
    raise RuntimeError(f"too many name collisions in {dest_dir} for {src.name}")


def _looks_like_text(path: Path) -> bool:
    """Return True if *path* appears to be a UTF-8 text file.

    Reads up to 8 KB and applies two cheap binary checks:
    - A NUL byte (``\\x00``) anywhere in the sample → binary.
    - Failure to decode the sample as UTF-8 → binary.

    Both checks cover the vast majority of common binary formats (images,
    archives, executables, compiled blobs).  All plain-text source files
    (.md, .py, .rs, .go, .yaml, .toml, .txt, etc.) pass both checks cleanly.
    """
    _SAMPLE = 8192
    try:
        sample = path.read_bytes()[:_SAMPLE]
    except OSError:
        return False
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


# ---------------------------------------------------------------------------
# ingest (headline command) -- the OUTER corpus sweep
# ---------------------------------------------------------------------------


def _print_summary(summary: list[tuple[str, str]]) -> None:
    print("\n--- ingest summary ---")
    for name, status in summary:
        mark = GREEN + "\u2713" if status == "converged" else YELLOW + "\u2022"
        print(f"  {mark}{RESET} {status:<14} {name}")
    if summary:
        failed_n = sum(
            1
            for _, s in summary
            if s in {"error", "not-converged", "tampered", "binary"}
        )
        converged_n = sum(1 for _, s in summary if s == "converged")
        print(f"  total={len(summary)}  converged={converged_n}  failed={failed_n}")


def ingest(
    wiki: str | Path = ".",
    *,
    source: str | Path | None = None,
    max_cycles: int | None = None,
    keep_going: bool = False,
) -> int:
    """Integrate inbox sources via the engine.

    Parameters
    ----------
    wiki:
        Wiki directory (resolved from cwd if relative).
    source:
        Path to a single source file.  When omitted the full inbox is drained.
    max_cycles:
        Convergence budget passed to the inner pipeline.  ``None`` means use
        the wiki policy default (or 3 if no policy is configured).
    keep_going:
        In single-file mode: continue to the next source after a failure.
        In drain mode: this flag is a documented NO-OP — failures always route
        to ``_failed/`` and draining always continues regardless.
    """
    wiki = Path(wiki).resolve()
    if not wiki.is_dir():
        _fail(f"wiki dir not found: {wiki} (run `wiki-weaver init {wiki}` first)")
        return 1

    inbox = wiki / INBOX
    archive = wiki / ARCHIVE
    inbox.mkdir(exist_ok=True)
    archive.mkdir(exist_ok=True)

    if source:
        # ------------------------------------------------------------------ #
        # SINGLE-FILE PATH — behavior UNCHANGED from original.                #
        # keep_going and exit-on-first-failure semantics are preserved here.  #
        # ------------------------------------------------------------------ #
        sources: list[Path] = [Path(source).resolve()]

        # Import the engine runner lazily so `doctor`/`init`/`lint` never pay
        # the cost of pulling in the attractor engine.
        from cli.engine_runner import run_inner

        processed = _processed_sources(wiki)
        summary: list[tuple[str, str]] = []

        for src in sources:
            name = src.name

            # Text sniff: fail loud on binary source; don't pollute the registry.
            if not _looks_like_text(src):
                _fail(f"{name}: unsupported binary source (no text handler)")
                summary.append((name, "binary"))
                _print_summary(summary)
                return 1

            # Fix 3: assign/look up a STABLE id by content hash BEFORE ingest
            # and dedupe an already-ingested source (same bytes) regardless of
            # filename.
            entry, is_new = _assign_source_id(wiki, src)
            source_id = entry["id"]
            file_hash = entry["hash"]
            already_done = entry.get("ingested") or name in processed
            if already_done:
                _warn(
                    f"skip (already ingested as source id [{source_id}], "
                    f"hash {file_hash[:12]}): {name}"
                )
                summary.append((name, "skipped"))
                continue
            if is_new:
                print(f"  assigned stable source id [{source_id}] for {name}")
            else:
                print(f"  reusing stable source id [{source_id}] for {name}")

            print(f"\n=== ingest: {name} (source id [{source_id}]) ===")

            # Fix 1b: snapshot process state so we can detect any agent-written
            # ledger line / archive move performed DURING the inner run (the lib
            # writes process state only AFTER this, on real convergence).
            before_state = _snapshot_process_state(wiki)

            try:
                result = run_inner(
                    src, wiki, max_cycles=max_cycles, source_id=source_id
                )
            except Exception as e:  # noqa: BLE001 -- surface the real failure, loudly
                _fail(f"engine error on {name}: {type(e).__name__}: {e}")
                summary.append((name, "error"))
                if not keep_going:
                    _print_summary(summary)
                    return 1
                continue

            # Fix 1b: never trust agent-written process state. Undo + fail loud.
            violations = _detect_and_undo_tamper(wiki, before_state)
            if violations:
                _fail(
                    f"{name}: TAMPER DETECTED -- the ingest agent wrote process "
                    f"state it does not own. Convergence is NOT trusted; "
                    f"fabricated records were reverted."
                )
                for v in violations:
                    _fail(f"    - {v}")
                summary.append((name, "tampered"))
                if not keep_going:
                    _print_summary(summary)
                    return 1
                continue

            if result.converged:
                dest = archive / name
                if src.is_file() and src.parent == inbox:
                    src.replace(dest)
                _append_ledger(
                    wiki,
                    {
                        "source": name,
                        "source_id": source_id,
                        "hash": file_hash,
                        "status": result.status,
                        "converged": result.converged,
                        "archived_to": str(dest),
                        "logs_dir": str(result.logs_dir),
                        "timestamp": datetime.now().isoformat(timespec="seconds"),
                    },
                )
                _mark_source_ingested(wiki, file_hash)
                _ok(f"{name}: converged (logs: {result.logs_dir})")
                summary.append((name, "converged"))
            else:
                _fail(
                    f"{name}: did not converge "
                    f"(status={result.status}, reason={result.failure_reason})"
                )
                summary.append((name, "not-converged"))
                if not keep_going:
                    _print_summary(summary)
                    return 1

        _print_summary(summary)
        return 0

    # ---------------------------------------------------------------------- #
    # INBOX DRAIN PATH — re-globs _inbox on every pass so files added mid-run #
    # are picked up automatically.                                            #
    #                                                                         #
    # Load-bearing invariant: every file picked from _inbox MUST leave        #
    # _inbox this pass.  This keeps the inbox strictly shrinking and          #
    # guarantees termination — no infinite spin on bad files.                 #
    #                                                                         #
    # Terminal dispositions:                                                  #
    #   converged   → _archive/  (existing behaviour)                        #
    #   duplicate   → _archive/  (collision-safe; was: left in inbox → spin) #
    #   error/tamper/non-convergence → _failed/ (new; was: halted the run)   #
    #                                                                         #
    # --keep-going is accepted but is a NO-OP in drain mode: failures always  #
    # route to _failed/ and draining always continues regardless.  The flag   #
    # no longer controls early-exit here; exit code is set after the drain.   #
    # ---------------------------------------------------------------------- #

    # Warn + bail early if inbox is empty (preserves original UX; lazy import).
    if not any(
        p for p in inbox.iterdir() if p.is_file() and not p.name.startswith(".")
    ):
        _warn(f"no sources to ingest (inbox empty: {inbox})")
        return 0

    # Import the engine runner lazily so `doctor`/`init`/`lint` never pay the
    # cost of pulling in the attractor engine.
    from cli.engine_runner import run_inner

    processed = _processed_sources(wiki)
    summary_drain: list[tuple[str, str]] = []

    failed_dir = wiki / FAILED
    failed_dir.mkdir(exist_ok=True)

    # Debounce: skip files written < 2 s ago (half-written by a concurrent
    # producer).  If all pending files are too-fresh, sleep briefly and retry
    # up to _FRESH_RETRIES_MAX times before declaring the drain complete.
    _DEBOUNCE_SECS = 2.0
    _FRESH_RETRIES_MAX = 5
    _fresh_retries = 0

    while True:
        pending = sorted(
            p for p in inbox.iterdir() if p.is_file() and not p.name.startswith(".")
        )
        now = time.time()
        ready = [p for p in pending if (now - p.stat().st_mtime) >= _DEBOUNCE_SECS]

        if not ready:
            if pending and _fresh_retries < _FRESH_RETRIES_MAX:
                # Files exist but all too-fresh; wait and retry.
                _fresh_retries += 1
                time.sleep(0.5)
                continue
            # No files at all, or fresh-retry budget exhausted → drain complete.
            break

        _fresh_retries = 0  # reset whenever we find a ready file
        src = ready[0]
        name = src.name

        # Text sniff: route binary files to _failed/ without calling run_inner.
        if not _looks_like_text(src):
            _fail(
                f"{src.name}: unsupported binary source (no text handler)"
                " — routing to _failed/"
            )
            _collision_safe_move(src, failed_dir)
            summary_drain.append((src.name, "binary"))
            continue

        # Fix 3: assign/look up a STABLE id by content hash BEFORE ingest and
        # dedupe an already-ingested source (same bytes) regardless of filename.
        entry, is_new = _assign_source_id(wiki, src)
        source_id = entry["id"]
        file_hash = entry["hash"]
        already_done = entry.get("ingested") or name in processed
        if already_done:
            _warn(
                f"skip (already ingested as source id [{source_id}], "
                f"hash {file_hash[:12]}): {name}"
            )
            # Drain mode: move dup out of inbox to clear it (prevents spin).
            _collision_safe_move(src, archive)
            summary_drain.append((name, "skipped"))
            continue
        if is_new:
            print(f"  assigned stable source id [{source_id}] for {name}")
        else:
            print(f"  reusing stable source id [{source_id}] for {name}")

        print(f"\n=== ingest: {name} (source id [{source_id}]) ===")

        # Fix 1b: snapshot process state so we can detect any agent-written
        # ledger line / archive move performed DURING the inner run (the lib
        # writes process state only AFTER this, on real convergence).
        before_state = _snapshot_process_state(wiki)

        try:
            result = run_inner(src, wiki, max_cycles=max_cycles, source_id=source_id)
        except Exception as e:  # noqa: BLE001 -- surface the real failure, loudly
            _fail(f"engine error on {name}: {type(e).__name__}: {e}")
            if src.is_file() and src.parent == inbox:
                _collision_safe_move(src, failed_dir)
            summary_drain.append((name, "error"))
            continue  # drain always continues; exit code is set after drain

        # Fix 1b: never trust agent-written process state. Undo + fail loud.
        violations = _detect_and_undo_tamper(wiki, before_state)
        if violations:
            _fail(
                f"{name}: TAMPER DETECTED -- the ingest agent wrote process "
                f"state it does not own. Convergence is NOT trusted; "
                f"fabricated records were reverted."
            )
            for v in violations:
                _fail(f"    - {v}")
            if src.is_file() and src.parent == inbox:
                _collision_safe_move(src, failed_dir)
            summary_drain.append((name, "tampered"))
            continue

        if result.converged:
            dest = archive / name
            if src.is_file() and src.parent == inbox:
                src.replace(dest)
            _append_ledger(
                wiki,
                {
                    "source": name,
                    "source_id": source_id,
                    "hash": file_hash,
                    "status": result.status,
                    "converged": result.converged,
                    "archived_to": str(dest),
                    "logs_dir": str(result.logs_dir),
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                },
            )
            _mark_source_ingested(wiki, file_hash)
            _ok(f"{name}: converged (logs: {result.logs_dir})")
            summary_drain.append((name, "converged"))
        else:
            _fail(
                f"{name}: did not converge "
                f"(status={result.status}, reason={result.failure_reason})"
            )
            if src.is_file() and src.parent == inbox:
                _collision_safe_move(src, failed_dir)
            summary_drain.append((name, "not-converged"))
            # Drain mode: always continue (never halt on non-convergence).

    _print_summary(summary_drain)
    # Fail-loud after the drain: nonzero if anything went to _failed/.
    failed_n = sum(
        1
        for _, s in summary_drain
        if s in {"error", "not-converged", "tampered", "binary"}
    )
    return 1 if failed_n else 0


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------


def lint(wiki: str | Path = ".") -> int:
    """Run the structural validator against a wiki directory."""
    wiki = Path(wiki).resolve()
    if not wiki.is_dir():
        _fail(f"wiki dir not found: {wiki}")
        return 1
    # Use the same validator config as the in-pipeline validate node so that
    # `wiki-weaver lint` and the pipeline `validate` step always agree.
    argv = [sys.executable, str(VALIDATE_PY), str(wiki)]
    validator_cfg = wiki / "policy" / "validator.yaml"
    if validator_cfg.is_file():
        argv += ["--config", str(validator_cfg)]
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
    )
    sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    return proc.returncode


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def doctor(*, wiki: str | Path | None = None) -> int:
    """Run environment diagnostics, optionally checking a specific wiki."""
    ok = True

    if os.environ.get("ANTHROPIC_API_KEY"):
        _ok("ANTHROPIC_API_KEY is set")
    else:
        _fail("ANTHROPIC_API_KEY is not set")
        ok = False

    # Engine runner imports cleanly (no engine cost yet).
    try:
        from cli.engine_runner import (
            ATTRACTOR_PIPELINE_LOCAL,
            load_ci_config,
        )
    except Exception as e:  # noqa: BLE001
        _fail(f"could not load engine_runner: {e}")
        return 1

    # foundation is the only hard import requirement; prepare() resolves the
    # loop-pipeline orchestrator and hook modules from the bundle on demand.
    try:
        import amplifier_foundation  # noqa: F401

        _ok("amplifier_foundation importable")
    except Exception as e:  # noqa: BLE001
        _fail(f"amplifier_foundation not importable: {e}")
        _warn("  run wiki-weaver under a python env that has amplifier-foundation")
        _warn("  (e.g. the interpreter behind ~/.local/bin/amplifier)")
        ok = False

    # unified_llm must be importable: the engine's DirectProviderBackend fallback
    # imports it, and a stale unified-llm-client (>=0.2 ships as `llm/`, not
    # `unified_llm/`) makes that fallback crash AFTER a multi-minute ingest with
    # ModuleNotFoundError. Catch the regression here in one second instead.
    try:
        import unified_llm  # noqa: F401

        _ok("unified_llm importable (engine fallback path safe)")
    except Exception as e:  # noqa: BLE001
        _fail(f"unified_llm NOT importable: {e}")
        _warn("  install the correct client: uv pip install --python <amplifier py> \\")
        _warn(
            "  --force-reinstall <attractor-cache>/modules/unified-llm-client (v0.1.x, ships unified_llm/)"
        )
        ok = False

    if ATTRACTOR_PIPELINE_LOCAL:
        pipeline_bundle = Path(ATTRACTOR_PIPELINE_LOCAL)
        if pipeline_bundle.is_file():
            _ok(f"attractor-pipeline bundle found: {pipeline_bundle}")
        else:
            _warn(
                f"WIKI_WEAVER_ATTRACTOR_PIPELINE set but path missing ({pipeline_bundle});"
                " will fall back to git URL"
            )
    else:
        _warn(
            "WIKI_WEAVER_ATTRACTOR_PIPELINE not set; will load attractor-pipeline from git URL"
        )

    # context-intelligence hook config (server_url + api_key) from settings.
    ci_cfg = load_ci_config()
    server_url = ci_cfg.get("context_intelligence_server_url")
    if ci_cfg.get("context_intelligence_api_key"):
        _ok("context-intelligence hook config found in settings (api_key + server_url)")
    else:
        _warn(
            "no context-intelligence api_key in settings; hook composes but fails soft"
        )

    # Probe the CI server (GET, short timeout). DOWN is OK -- the hook fails soft
    # and still writes local events.jsonl. No hardcoded default: if the user has
    # not configured a server in settings, there is nothing to probe.
    if not server_url:
        _warn("no context-intelligence server_url in settings; skipping probe")
    else:
        try:
            import urllib.request

            with urllib.request.urlopen(server_url, timeout=3) as resp:  # noqa: S310
                _ok(
                    f"context-intelligence server UP at {server_url} (HTTP {resp.status})"
                )
        except Exception as e:  # noqa: BLE001
            _warn(
                f"context-intelligence server DOWN/unreachable at {server_url} ({type(e).__name__}); OK -- hook fails soft, local events.jsonl still written"
            )

    if VALIDATE_PY.is_file():
        _ok(f"structural validator found: {VALIDATE_PY}")
    else:
        _fail(f"validate_wiki.py missing: {VALIDATE_PY}")
        ok = False

    if wiki:
        wiki_path = Path(wiki).resolve()
        missing = [
            d for d in (INBOX, ARCHIVE, ".ai/feedback") if not (wiki_path / d).is_dir()
        ]
        if wiki_path.is_dir() and not missing:
            _ok(f"wiki structure OK: {wiki_path}")
        else:
            _fail(f"wiki structure incomplete at {wiki_path} (missing: {missing})")
            ok = False

        # Policy echo: show the resolved paths + model knobs for this wiki so the
        # user can verify that project overrides are being picked up correctly.
        if wiki_path.is_dir():
            try:
                from cli.policy import load_policy

                policy = load_policy(wiki_path)
                _ok(f"  policy.schema:          {policy.schema_path}")
                _ok(f"  policy.rubric:          {policy.convergence_rubric_path}")
                _ok(f"  policy.inner_dot:       {policy.inner_dot_path}")
                _ok(
                    f"  policy.validator_cfg:   "
                    f"{policy.validator_config_path or '(built-in defaults)'}"
                )
                _ok(f"  policy.provider:        {policy.provider}")
                _ok(f"  policy.models:          {policy.models}")
                _ok(f"  policy.max_cycles:      {policy.max_cycles}")
                _warn(
                    f"  policy.parallelism:     {policy.parallelism}"
                    " (RESERVED \u2014 within-wiki ingest is sequential;"
                    " parallelism key accepted but always honored as 1)"
                )
            except Exception as e:  # noqa: BLE001
                _warn(f"  could not resolve policy for {wiki_path}: {e}")

    print()
    if ok:
        _ok("doctor: all required checks passed")
        return 0
    _fail("doctor: one or more checks failed")
    return 1


# ---------------------------------------------------------------------------
# query (minimal stub)
# ---------------------------------------------------------------------------


def query(wiki: str | Path, term: str) -> int:
    """List pages in ``wiki`` that contain ``term`` (case-insensitive stub)."""
    wiki_path = Path(wiki).resolve()
    if not wiki_path.is_dir():
        _fail(f"wiki dir not found: {wiki_path}")
        return 1
    term_lower = term.lower()
    hits = 0
    for page in sorted(wiki_path.glob("*.md")):
        text = page.read_text(encoding="utf-8", errors="replace")
        if term_lower in text.lower():
            print(f"  {page.name}")
            hits += 1
    print(f"\n{hits} page(s) match {term!r} (query is a minimal stub)")
    return 0


# ---------------------------------------------------------------------------
# ask -- read the compiled wiki and answer a question (Phase B)
# ---------------------------------------------------------------------------
#
# MECHANISM (structural, not instructional): the spawned agent's tools are
# constrained in engine_runner.make_ask_spawn_fn so it structurally cannot
# write files or fetch from the web — only read within the wiki directory.
# This forces grounding in wiki content and makes fail-loud-on-absent the
# natural outcome (the agent can't pull from elsewhere).


def ask(
    wiki: str | Path = ".",
    question: str = "",
    *,
    json_out: bool = False,
) -> int:
    """Answer a question by reading the compiled wiki (no embeddings)."""
    import json as _json

    wiki_path = Path(wiki).resolve()
    if not wiki_path.is_dir():
        _fail(f"wiki dir not found: {wiki_path}")
        return 1

    from cli.engine_runner import run_ask

    _warn(f"asking wiki at {wiki_path!r}: {question!r}")
    try:
        result = run_ask(wiki_path, question)
    except Exception as e:  # noqa: BLE001
        _fail(f"ask error: {type(e).__name__}: {e}")
        return 1

    if json_out:
        print(
            _json.dumps(
                {
                    "answer": result.answer,
                    "pages_used": result.pages_used,
                    "refused": result.refused,
                },
                indent=2,
            )
        )
    else:
        print(result.answer)
        if result.pages_used:
            print(f"\nPages consulted: {', '.join(result.pages_used)}")
    return 0


# ---------------------------------------------------------------------------
# rag -- naive-RAG baseline: answer from raw source articles (Phase B A/B)
# ---------------------------------------------------------------------------
#
# Variant B of the A/B comparison: the SAME mechanism as ask (bash/web removed,
# writes denied, reads scoped) but pointed at the RAW article directory instead
# of the compiled wiki. The only variable is synthesis.


def rag(
    articles: str | Path = "~/medium_articles",
    question: str = "",
    *,
    json_out: bool = False,
) -> int:
    """Naive-RAG baseline: answer from raw source articles (A/B variant B)."""
    import json as _json

    articles_path = Path(articles).expanduser().resolve()
    if not articles_path.is_dir():
        _fail(f"articles dir not found: {articles_path}")
        return 1

    from cli.engine_runner import run_rag

    _warn(f"RAG baseline over articles at {articles_path!r}: {question!r}")
    try:
        result = run_rag(articles_path, question)
    except Exception as e:  # noqa: BLE001
        _fail(f"rag error: {type(e).__name__}: {e}")
        return 1

    if json_out:
        print(
            _json.dumps(
                {
                    "answer": result.answer,
                    "pages_used": result.pages_used,
                    "refused": result.refused,
                },
                indent=2,
            )
        )
    else:
        print(result.answer)
        if result.pages_used:
            print(f"\nArticles consulted: {', '.join(result.pages_used)}")
    return 0
