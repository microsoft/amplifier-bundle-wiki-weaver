#!/usr/bin/env python3
"""One-time fixer: normalize [[Display Title]] -> [[slug|Display Title]] so the
wiki's Obsidian-style links actually resolve in Obsidian.

Why this is provably correct: it delegates to pipeline/normalize_links.py which
reuses the *exact* `_slug` and frontmatter parsing from pipeline/validate_wiki.py
and builds the *same* alias map the validator uses (filename-slug AND
frontmatter-title-slug -> canonical page). Because the wikis already PASS the
validator, every existing [[link]] target already slugs to a real page -- so
rewriting the target to that real filename stem is guaranteed to point at an
existing file and stay validator-valid.

Usage:
    python scripts/fix_wiki_links.py <wiki_dir> [<wiki_dir> ...]            # dry-run
    python scripts/fix_wiki_links.py <wiki_dir> [<wiki_dir> ...] --apply   # writes (after backup)
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

# Delegate to the shared normalizer core — NO duplicate slug/alias logic here.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from pipeline.normalize_links import _build_alias_map, _normalize_text  # noqa: E402


def process_wiki(wiki: Path, apply: bool) -> None:
    pages = sorted(wiki.glob("*.md"))
    if not pages:
        print(f"  {wiki}: no .md pages")
        return
    amap = _build_alias_map(pages)
    files_changed = 0
    total_rewrites = 0
    all_unresolved: list[str] = []
    changes: list[tuple[Path, str]] = []
    for p in pages:
        text = p.read_text(encoding="utf-8", errors="replace")
        new, n, unresolved = _normalize_text(text, amap)
        all_unresolved.extend(unresolved)
        if new != text:
            files_changed += 1
            total_rewrites += n
            changes.append((p, new))

    print(f"  {wiki}")
    print(
        f"     pages={len(pages)}  files_to_change={files_changed}  links_rewritten={total_rewrites}"
    )
    uniq_unres = sorted(set(all_unresolved))
    print(f"     unresolved targets (left untouched): {len(uniq_unres)}")
    for u in uniq_unres[:10]:
        print(f"        ? [[{u}]]")

    if apply and changes:
        backup = wiki.parent / f"{wiki.name}.bak-{datetime.now():%Y%m%d-%H%M%S}"
        shutil.copytree(wiki, backup)
        for p, new in changes:
            p.write_text(new, encoding="utf-8")
        print(f"     APPLIED. backup -> {backup}")
    elif apply:
        print("     APPLIED: nothing to change.")


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--apply"]
    apply = "--apply" in sys.argv
    if not args:
        print(__doc__)
        return 2
    print(f"=== fix_wiki_links ({'APPLY' if apply else 'DRY-RUN'}) ===")
    for d in args:
        process_wiki(Path(d), apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
