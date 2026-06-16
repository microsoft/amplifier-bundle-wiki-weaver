#!/usr/bin/env python3
"""Normalize [[Display Title]] -> [[slug|Display Title]] wikilinks in-place.

Shared normalizer core for the wiki-weaver pipeline and standalone use.

Public API:
    normalize_wiki(wiki_dir) -> (files_changed, links_rewritten, unresolved)
        Normalizes all [[links]] IN-PLACE, no backup created. Idempotent.

Reuses validate_wiki._slug and _parse_frontmatter as the SINGLE SOURCE OF TRUTH
for slug computation and frontmatter parsing — no duplicate logic across the repo.

Why this is provably correct: it builds the same alias map the validator uses
(filename-slug AND frontmatter-title-slug -> canonical page).  Because wikis
already pass the validator, every [[link]] target already slugs to a real page;
rewriting to [[slug|Title]] is guaranteed to point at an existing file and stay
validator-valid.

CLI usage (in-pipeline mode — no backup, always exits 0):
    python pipeline/normalize_links.py <wiki_dir>

CLI usage (standalone / one-time mode — creates a dated backup first):
    python pipeline/normalize_links.py <wiki_dir> --backup
"""

from __future__ import annotations

import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Single source of truth: import slug + frontmatter parsing from validate_wiki.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from pipeline.validate_wiki import _parse_frontmatter, _slug  # noqa: E402

# Capture the full inside of [[ ... ]] — we parse target / section / display.
_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")


def _build_alias_map(pages: list[Path]) -> dict[str, str]:
    """Build slug(target) -> real filename stem, mirroring validate_wiki's alias_to_page.

    Two-pass: filename slugs first (authoritative), then frontmatter title slugs
    (setdefault so filename wins on collision).
    """
    amap: dict[str, str] = {}
    for p in pages:
        stem = p.stem
        amap[_slug(stem)] = stem  # filename-slug -> real stem (authoritative)
    for p in pages:
        fm = _parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        if fm and fm.get("title"):
            title = fm["title"].strip().strip('"').strip("'")
            amap.setdefault(_slug(title), p.stem)  # title-slug -> stem (no override)
    return amap


def _normalize_text(text: str, amap: dict[str, str]) -> tuple[str, int, list[str]]:
    """Rewrite [[Title]] -> [[slug|Title]] (and [[Title#sec]] -> [[slug#sec|Title]]).

    Returns (new_text, rewrites_count, unresolved_targets).
    Already-correct [[slug|Title]] links pass through untouched (idempotent).
    Unresolvable targets are left unchanged and accumulated in the returned list.
    """
    rewrites = 0
    unresolved: list[str] = []

    def repl(m: re.Match[str]) -> str:
        nonlocal rewrites
        inner = m.group(1)
        # Split off display portion (after |).
        target_part, display = (
            (inner.split("|", 1) + [None])[:2] if "|" in inner else (inner, None)
        )
        # Split off section anchor (after #).
        if "#" in target_part:
            target, sec = target_part.split("#", 1)
            section = "#" + sec
        else:
            target, section = target_part, ""
        target = target.strip()
        stem = amap.get(_slug(target))
        if stem is None:
            unresolved.append(target)
            return m.group(0)  # leave untouched
        disp = (display if display is not None else target).strip()
        new = f"[[{stem}{section}]]" if disp == stem else f"[[{stem}{section}|{disp}]]"
        if new != m.group(0):
            rewrites += 1
        return new

    return _WIKILINK.sub(repl, text), rewrites, unresolved


def normalize_wiki(wiki_dir: Path | str) -> tuple[int, int, list[str]]:
    """Normalize all [[links]] in wiki_dir IN-PLACE.  No backup created.

    Returns (files_changed, links_rewritten, sorted_unresolved_targets).
    Idempotent: a second run on an already-normalized wiki returns (0, 0, []).
    """
    wiki_dir = Path(wiki_dir)
    pages = sorted(wiki_dir.glob("*.md"))
    if not pages:
        return 0, 0, []

    amap = _build_alias_map(pages)
    files_changed = 0
    total_rewrites = 0
    all_unresolved: list[str] = []

    for p in pages:
        text = p.read_text(encoding="utf-8", errors="replace")
        new_text, n, unresolved = _normalize_text(text, amap)
        all_unresolved.extend(unresolved)
        if new_text != text:
            files_changed += 1
            total_rewrites += n
            p.write_text(new_text, encoding="utf-8")

    return files_changed, total_rewrites, sorted(set(all_unresolved))


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--backup"]
    backup = "--backup" in sys.argv
    if not args:
        print(__doc__)
        return 2

    wiki_dir = Path(args[0])
    if not wiki_dir.is_dir():
        print(f"FAIL: wiki dir not found: {wiki_dir}", file=sys.stderr)
        return 1

    if backup:
        bak = wiki_dir.parent / f"{wiki_dir.name}.bak-{datetime.now():%Y%m%d-%H%M%S}"
        shutil.copytree(wiki_dir, bak)
        print(f"backup -> {bak}")

    files_changed, links_rewritten, unresolved = normalize_wiki(wiki_dir)
    print(
        f"normalize_wiki: files_changed={files_changed}"
        f" links_rewritten={links_rewritten}"
        f" unresolved={len(unresolved)}"
    )
    if unresolved:
        for u in unresolved[:10]:
            print(f"  ? [[{u}]]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
