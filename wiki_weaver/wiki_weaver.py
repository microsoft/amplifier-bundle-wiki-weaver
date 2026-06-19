# pyright: reportMissingImports=false
#!/usr/bin/env python3
"""wiki-weaver CLI.

Thin argparse wrapper around the importable lib API (wiki_weaver.lib).

Subcommands:
    init <wiki_dir>            scaffold a fresh wiki
    ingest [--wiki] [--source] integrate inbox sources via the engine
    lint   [--wiki]            run the structural validator
    doctor                     environment diagnostics
    query  [--wiki] <q>        (stub) list pages matching a term
    ask    <question> [--wiki] answer a question by reading the compiled wiki
"""

from __future__ import annotations

import argparse

from wiki_weaver import __version__

# ---------------------------------------------------------------------------
# Re-exports: symbols imported by tests from wiki_weaver.wiki_weaver (backward compat)
# ---------------------------------------------------------------------------
from wiki_weaver.lib import (
    ARCHIVE,
    FAILED,
    INBOX,
    REGISTRY_NAME,
    _assign_source_id,
    _parse_transcript_header,
    _read_source_frontmatter,
    ask,
    doctor,
    ingest,
    init,
    lint,
    query,
)

__all__ = [
    # constants (test imports)
    "ARCHIVE",
    "FAILED",
    "INBOX",
    "REGISTRY_NAME",
    # helpers (test imports)
    "_assign_source_id",
    "_parse_transcript_header",
    "_read_source_frontmatter",
    # clean lib API (re-exported for convenience)
    "init",
    "ingest",
    "lint",
    "doctor",
    "query",
    "ask",
]


# ---------------------------------------------------------------------------
# cmd_* wrappers: unpack argparse.Namespace → call lib function
# ---------------------------------------------------------------------------
# These stay here (not in lib) so they remain importable from wiki_weaver.wiki_weaver,
# which is what existing tests and the main() dispatch expect.


def cmd_init(args: argparse.Namespace) -> int:
    from wiki_weaver.engine_runner import run_init

    return run_init(
        args.wiki_dir,
        purpose=args.purpose,
        sample_inbox=not args.no_sample_inbox,
        plain=args.plain,
    )


def cmd_ingest(args: argparse.Namespace) -> int:
    return ingest(
        args.wiki,
        source=args.source,
        max_cycles=args.max_cycles,
        keep_going=args.keep_going,
    )


def cmd_lint(args: argparse.Namespace) -> int:
    from wiki_weaver.engine_runner import run_lint

    return run_lint(args.wiki)


def cmd_doctor(args: argparse.Namespace) -> int:
    return doctor(wiki=args.wiki)


def cmd_query(args: argparse.Namespace) -> int:
    return query(args.wiki, args.term)


def cmd_ask(args: argparse.Namespace) -> int:
    return ask(args.wiki, args.question, json_out=args.json_out)


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

    p_init = sub.add_parser(
        "init", help="scaffold a wiki directory and design its schema"
    )
    p_init.add_argument("wiki_dir")
    p_init.add_argument(
        "--purpose",
        default=None,
        metavar="TEXT",
        help=(
            "Rich free-text description of the wiki's intended use and desired outcomes. "
            "When provided (and ANTHROPIC_API_KEY is set), the LLM designs a domain-fit "
            "schema and writes it to <wiki>/policy/schema.md. "
            "Example: --purpose 'AI coding tools second brain for answering which tool "
            "to use for X and comparing alternatives'"
        ),
    )
    p_init.add_argument(
        "--plain",
        action="store_true",
        help="scaffold only — no LLM schema design, use the generic built-in schema",
    )
    p_init.add_argument(
        "--no-sample-inbox",
        action="store_true",
        dest="no_sample_inbox",
        help="do not sample existing _inbox/ sources to inform schema design",
    )

    p_ingest = sub.add_parser("ingest", help="integrate inbox sources via the engine")
    p_ingest.add_argument("--wiki", default=".", help="wiki directory (default: .)")
    p_ingest.add_argument("--source", default=None, help="ingest a single source file")
    p_ingest.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="convergence budget (default: from wiki.config.yaml or 3)",
    )
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

    p_query = sub.add_parser(
        "query",
        help="naive substring page search; for real cited answers use 'ask'",
    )
    p_query.add_argument("term")
    p_query.add_argument("--wiki", default=".", help="wiki directory (default: .)")

    p_ask = sub.add_parser(
        "ask", help="answer a question by reading the compiled wiki (no embeddings)"
    )
    p_ask.add_argument("question", help="question to answer")
    p_ask.add_argument("--wiki", default=".", help="wiki directory (default: .)")
    p_ask.add_argument(
        "--json",
        dest="json_out",
        action="store_true",
        help="output JSON: {answer, pages_used, refused}",
    )

    args = parser.parse_args()

    dispatch = {
        "init": cmd_init,
        "ingest": cmd_ingest,
        "lint": cmd_lint,
        "doctor": cmd_doctor,
        "query": cmd_query,
        "ask": cmd_ask,
    }
    if args.command is None:
        parser.print_help()
        raise SystemExit(0)
    raise SystemExit(dispatch[args.command](args))


if __name__ == "__main__":
    main()
