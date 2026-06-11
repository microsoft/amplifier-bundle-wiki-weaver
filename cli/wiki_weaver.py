# pyright: reportMissingImports=false
#!/usr/bin/env python3
"""wiki-weaver CLI.

An LLM-wiki ingest tool. The ATTRACTOR ENGINE runs the inner convergence
pipeline for ONE source (ingest -> validate -> assess -> feedback -> loop).
The OUTER corpus sweep is this plain Python loop: for each source in the
wiki's ``_inbox/``, run the inner pipeline via the engine, and on success
archive the source and append a ledger line. Idempotent via the ledger.

Subcommands:
    init <wiki_dir>            scaffold a fresh wiki
    ingest [--wiki] [--source] integrate inbox sources via the engine
    lint   [--wiki]            run the structural validator
    doctor                     environment diagnostics
    query  [--wiki] <q>        (stub) list pages matching a term
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

from cli import __version__

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"

LEDGER_NAME = ".processed.jsonl"
INBOX = "_inbox"
ARCHIVE = "_archive"

# Pipeline assets live alongside this package's repo.
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


def cmd_init(args: argparse.Namespace) -> int:
    wiki = Path(args.wiki_dir).resolve()
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
# registry at <wiki>/.sources.json is the single source of truth: the CLILEDGER
# assigns/looks up a stable id by CONTENT HASH *before* ingest and threads it
# into the inner pipeline as $source_id. An already-ingested source (same hash)
# is deduped and skipped.

REGISTRY_NAME = ".sources.json"


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
    """
    file_hash = _source_hash(src)
    registry = _load_registry(wiki)
    existing = _registry_entry_for_hash(registry, file_hash)
    if existing is not None:
        return existing, False

    entry = {
        "id": int(registry["next_id"]),
        "filename": src.name,
        "hash": file_hash,
        "first_seen": datetime.now().isoformat(timespec="seconds"),
        "ingested": False,
    }
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
    # the CLI appends only after this guard runs. Truncate back to before_lines.
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
# ingest (headline command) -- the OUTER corpus sweep
# ---------------------------------------------------------------------------


def cmd_ingest(args: argparse.Namespace) -> int:
    wiki = Path(args.wiki).resolve()
    if not wiki.is_dir():
        _fail(f"wiki dir not found: {wiki} (run `wiki-weaver init {wiki}` first)")
        return 1

    inbox = wiki / INBOX
    archive = wiki / ARCHIVE
    inbox.mkdir(exist_ok=True)
    archive.mkdir(exist_ok=True)

    if args.source:
        sources = [Path(args.source).resolve()]
    else:
        sources = sorted(p for p in inbox.glob("*.md") if p.is_file())

    if not sources:
        _warn(f"no sources to ingest (inbox empty: {inbox})")
        return 0

    # Import the engine runner lazily so `doctor`/`init`/`lint` never pay the
    # cost of pulling in the attractor engine.
    from cli.engine_runner import run_inner

    processed = _processed_sources(wiki)
    summary: list[tuple[str, str]] = []

    for src in sources:
        name = src.name

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
            summary.append((name, "skipped"))
            continue
        if is_new:
            print(f"  assigned stable source id [{source_id}] for {name}")
        else:
            print(f"  reusing stable source id [{source_id}] for {name}")

        print(f"\n=== ingest: {name} (source id [{source_id}]) ===")

        # Fix 1b: snapshot process state so we can detect any agent-written
        # ledger line / archive move performed DURING the inner run (the CLI
        # writes process state only AFTER this, on real convergence).
        before_state = _snapshot_process_state(wiki)

        try:
            result = run_inner(
                src, wiki, max_cycles=args.max_cycles, source_id=source_id
            )
        except Exception as e:  # noqa: BLE001 -- surface the real failure, loudly
            _fail(f"engine error on {name}: {type(e).__name__}: {e}")
            summary.append((name, "error"))
            if not args.keep_going:
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
            if not args.keep_going:
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
            if not args.keep_going:
                _print_summary(summary)
                return 1

    _print_summary(summary)
    return 0


def _print_summary(summary: list[tuple[str, str]]) -> None:
    print("\n--- ingest summary ---")
    for name, status in summary:
        mark = GREEN + "\u2713" if status == "converged" else YELLOW + "\u2022"
        print(f"  {mark}{RESET} {status:<14} {name}")


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------


def cmd_lint(args: argparse.Namespace) -> int:
    wiki = Path(args.wiki).resolve()
    if not wiki.is_dir():
        _fail(f"wiki dir not found: {wiki}")
        return 1
    proc = subprocess.run(
        [sys.executable, str(VALIDATE_PY), str(wiki)],
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


def cmd_doctor(args: argparse.Namespace) -> int:
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

    pipeline_bundle = Path(ATTRACTOR_PIPELINE_LOCAL)
    if pipeline_bundle.is_file():
        _ok(f"attractor-pipeline bundle found: {pipeline_bundle}")
    else:
        _warn(
            f"local attractor-pipeline missing ({pipeline_bundle}); will fall back to git URL"
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

    if args.wiki:
        wiki = Path(args.wiki).resolve()
        missing = [
            d for d in (INBOX, ARCHIVE, ".ai/feedback") if not (wiki / d).is_dir()
        ]
        if wiki.is_dir() and not missing:
            _ok(f"wiki structure OK: {wiki}")
        else:
            _fail(f"wiki structure incomplete at {wiki} (missing: {missing})")
            ok = False

    print()
    if ok:
        _ok("doctor: all required checks passed")
        return 0
    _fail("doctor: one or more checks failed")
    return 1


# ---------------------------------------------------------------------------
# query (minimal stub)
# ---------------------------------------------------------------------------


def cmd_query(args: argparse.Namespace) -> int:
    wiki = Path(args.wiki).resolve()
    if not wiki.is_dir():
        _fail(f"wiki dir not found: {wiki}")
        return 1
    term = args.term.lower()
    hits = 0
    for page in sorted(wiki.glob("*.md")):
        text = page.read_text(encoding="utf-8", errors="replace")
        if term in text.lower():
            print(f"  {page.name}")
            hits += 1
    print(f"\n{hits} page(s) match {args.term!r} (query is a minimal stub)")
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="wiki-weaver",
        description="LLM-wiki ingest pipeline driven by the attractor engine.",
    )
    parser.add_argument(
        "--version", action="version", version=f"wiki-weaver {__version__}"
    )
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="scaffold a fresh wiki directory")
    p_init.add_argument("wiki_dir")

    p_ingest = sub.add_parser("ingest", help="integrate inbox sources via the engine")
    p_ingest.add_argument("--wiki", default=".", help="wiki directory (default: .)")
    p_ingest.add_argument("--source", default=None, help="ingest a single source file")
    p_ingest.add_argument("--max-cycles", type=int, default=3)
    p_ingest.add_argument(
        "--keep-going",
        action="store_true",
        help="continue to next source after a failure",
    )

    p_lint = sub.add_parser("lint", help="run the structural validator")
    p_lint.add_argument("--wiki", default=".", help="wiki directory (default: .)")

    p_doctor = sub.add_parser("doctor", help="environment diagnostics")
    p_doctor.add_argument(
        "--wiki", default=None, help="also check this wiki's structure"
    )

    p_query = sub.add_parser("query", help="(stub) list pages matching a term")
    p_query.add_argument("term")
    p_query.add_argument("--wiki", default=".", help="wiki directory (default: .)")

    args = parser.parse_args()

    dispatch = {
        "init": cmd_init,
        "ingest": cmd_ingest,
        "lint": cmd_lint,
        "doctor": cmd_doctor,
        "query": cmd_query,
    }
    if args.command is None:
        parser.print_help()
        raise SystemExit(0)
    raise SystemExit(dispatch[args.command](args))


if __name__ == "__main__":
    main()
