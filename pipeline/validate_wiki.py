#!/usr/bin/env python3
"""Structural validator for a woven wiki (Tier-1 graders).

ONE artifact, TWO jobs (DRY, per the eval rubric):
  1. The wiki-weaver pipeline's `validate` node (tool_command) — exit non-zero
     to force a refine cycle when the wiki is structurally broken.
  2. The eval's Tier-1 structural grader.

Checks (all deterministic — no LLM, no judgment):
  S1 link-integrity   every [[wikilink]] resolves to a page (or an explicit stub)
  S2 no-orphans       every content page has >=1 inbound link
  S3 frontmatter      every page has title, type, sources[]
  S5 source-provenance every content page cites >=1 source

Fails LOUD: prints what's broken and exits 1. No silent pass.

Usage:
    python validate_wiki.py <wiki_dir>
    python validate_wiki.py <wiki_dir> --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")
FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
# Pages exempt from the orphan check (navigation roots).
NAV_PAGES = {"index", "overview", "readme", "log"}
REQUIRED_FM = ("title", "type", "sources")
# Page types that are navigation/meta and need not cite a source.
META_TYPES = {"index", "overview", "log", "meta"}


def _slug(name: str) -> str:
    """Normalize a page name / link target to a comparison key."""
    return name.strip().lower().replace(" ", "-").replace("_", "-")


def _parse_frontmatter(text: str) -> dict | None:
    m = FRONTMATTER.match(text)
    if not m:
        return None
    fm: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip().lower()] = v.strip()
    return fm


def validate(wiki_dir: Path, *, config: dict | None = None) -> dict:
    """Validate the structural integrity of a wiki directory.

    ``config`` — optional dict overriding the built-in policy constants:
      ``nav_pages``            list[str] — orphan-exempt page slugs
      ``required_frontmatter`` list[str] — keys every page must carry
      ``meta_types``           list[str] — types exempt from source-citation check

    When ``config`` is None (the default), the built-in module-level constants
    (NAV_PAGES, REQUIRED_FM, META_TYPES) are used unchanged — backward-compatible.
    """
    # Resolve policy-driven constants: project config overrides built-in defaults.
    if config:
        nav_pages: set[str] = set(config.get("nav_pages") or NAV_PAGES)
        required_fm: tuple[str, ...] = tuple(
            config.get("required_frontmatter") or REQUIRED_FM
        )
        meta_types: set[str] = set(config.get("meta_types") or META_TYPES)
    else:
        nav_pages = NAV_PAGES
        required_fm = REQUIRED_FM
        meta_types = META_TYPES

    pages = sorted(wiki_dir.glob("*.md"))
    result: dict = {
        "wiki_dir": str(wiki_dir),
        "page_count": len(pages),
        "checks": {},
        "failures": [],
    }
    if not pages:
        result["failures"].append("no .md pages found in wiki dir")
        result["passed"] = False
        return result

    # A page is addressable by BOTH its filename slug and its frontmatter-title
    # slug. Obsidian-style [[links]] resolve by note title/name, not file path,
    # so resolving against only the filename causes false "broken link" reports
    # when the generator links by title but files are slugged (prompt/validator
    # drift). Index both aliases -> one canonical page id.
    parsed: dict[Path, dict | None] = {}
    alias_to_page: dict[str, str] = {}
    inbound: dict[str, int] = {}
    for p in pages:
        fm = _parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        parsed[p] = fm
        page_id = _slug(p.stem)
        inbound[page_id] = 0
        alias_to_page[page_id] = page_id
        if fm and fm.get("title"):
            title = fm["title"].strip().strip('"').strip("'")
            alias_to_page[_slug(title)] = page_id

    missing_fm: list[str] = []
    bad_fm: list[str] = []
    no_source: list[str] = []
    broken_links: list[str] = []
    obsidian_broken: list[str] = []  # S1b: target not a direct filename-stem match

    for p in pages:
        text = p.read_text(encoding="utf-8", errors="replace")
        fm = parsed[p]
        if fm is None:
            missing_fm.append(p.name)
        else:
            missing = [k for k in required_fm if k not in fm]
            if missing:
                bad_fm.append(f"{p.name} (missing: {', '.join(missing)})")
            ptype = fm.get("type", "").strip().lower()
            srcs = fm.get("sources", "").strip().strip("[]").strip()
            if ptype not in meta_types and not srcs:
                no_source.append(p.name)

        # Link resolution: count inbound (by canonical page id), flag unresolved.
        for tgt in WIKILINK.findall(text):
            page_id = alias_to_page.get(_slug(tgt))
            if page_id is not None:
                inbound[page_id] += 1
            else:
                broken_links.append(f"{p.name} -> [[{tgt}]]")
            # S1b: Obsidian-resolvability — target must be a DIRECT filename-stem
            # match so Obsidian can resolve the link without slug awareness.
            # After `normalize` runs in-pipeline all links are [[slug|Title]],
            # so in-pipeline wikis always satisfy this; the gate closes the
            # "validator-valid ≠ Obsidian-clickable" divergence permanently.
            if not (wiki_dir / (tgt + ".md")).exists():
                obsidian_broken.append(f"{p.name} -> [[{tgt}]]")

    orphans = [k for k, n in inbound.items() if n == 0 and k not in nav_pages]

    result["checks"] = {
        "S1_link_integrity": {
            "broken": len(broken_links),
            "detail": broken_links[:20],
        },
        "S1b_obsidian_resolvability": {
            "broken": len(obsidian_broken),
            "detail": obsidian_broken[:20],
        },
        "S2_no_orphans": {"orphans": len(orphans), "detail": sorted(orphans)[:20]},
        "S3_frontmatter": {
            "missing": missing_fm,
            "invalid": bad_fm,
        },
        "S5_provenance": {"uncited": no_source},
    }
    if broken_links:
        result["failures"].append(f"S1: {len(broken_links)} unresolved wikilink(s)")
    if obsidian_broken:
        result["failures"].append(
            f"S1b: {len(obsidian_broken)} link(s) not directly Obsidian-resolvable"
            " (use [[slug|Title]] form)"
        )
    if orphans:
        result["failures"].append(f"S2: {len(orphans)} orphan page(s)")
    if missing_fm:
        result["failures"].append(f"S3: {len(missing_fm)} page(s) missing frontmatter")
    if bad_fm:
        result["failures"].append(f"S3: {len(bad_fm)} page(s) with invalid frontmatter")
    if no_source:
        result["failures"].append(
            f"S5: {len(no_source)} content page(s) cite no source"
        )

    result["passed"] = not result["failures"]
    return result


def _render_report(r: dict) -> str:
    """Human-readable report (the same text printed to stdout).

    Reused for ``--out`` so downstream pipeline nodes can read the verbatim
    validator result from a file.
    """
    lines = [f"Wiki: {r['wiki_dir']}  ({r['page_count']} pages)"]
    for cid, c in r["checks"].items():
        lines.append(f"  {cid}: {c}")
    if r["passed"]:
        lines.append("PASS — structurally valid")
    else:
        lines.append("FAIL:")
        for f in r["failures"]:
            lines.append(f"  - {f}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wiki_dir", type=Path)
    ap.add_argument("--json", action="store_true")
    # --out: ALWAYS write the structured result (PASS or FAIL) to this file so
    # downstream pipeline nodes (feedback, refine-ingest) can READ the exact
    # validator failures. Dotted context keys are silently dropped in box-node
    # prompts, so a file is the reliable hand-off channel (PIPELINE_DESIGN.md
    # §4). Exit code is unchanged: 0 on pass, 1 on fail.
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="also write the structured result to this file (JSON if --json)",
    )
    # --config: project-supplied YAML overriding nav_pages/required_frontmatter/
    # meta_types.  When absent, the built-in module defaults are used unchanged.
    ap.add_argument(
        "--config",
        type=Path,
        default=None,
        help="YAML file overriding nav_pages, required_frontmatter, meta_types",
    )
    args = ap.parse_args()

    if not args.wiki_dir.is_dir():
        print(f"FAIL: wiki dir not found: {args.wiki_dir}", file=sys.stderr)
        return 1

    # Load project validator config if given (tolerant: empty dict on any failure).
    config: dict | None = None
    if args.config is not None:
        try:
            import yaml  # pyright: ignore[reportMissingModuleSource]

            config = yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            config = {}

    r = validate(args.wiki_dir, config=config)
    if args.json:
        rendered = json.dumps(r, indent=2) + "\n"
        print(rendered, end="")
    else:
        rendered = _render_report(r)
        print(rendered, end="")

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")

    return 0 if r["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
