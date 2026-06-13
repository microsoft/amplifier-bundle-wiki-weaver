# Wiki Schema (the contract every wiki page obeys)

This is the "schema layer" of the LLM-Wiki pattern — the rules the weaving agent
follows so the wiki stays coherent and machine-checkable. The structural
validator (`validate_wiki.py`) enforces the mechanical parts; the `assess` node
judges the rest against the eval rubric.

## Page = one markdown file per concept

`wiki/<slug>.md`. ONE page per canonical concept/entity — never one page per
source. When a new source discusses an existing concept, **update that page**,
don't create a near-duplicate.

## Frontmatter (required on every page)

```yaml
---
title: Human Readable Title
type: concept | entity | comparison | synthesis | source-summary | index | overview
sources: [1, 3, 4]        # source article numbers this page draws from
last_updated: 2026-06-10
confidence: 0.0-1.0        # optional; lower when sources disagree
stale: false               # optional
---
```

`validate_wiki.py` requires `title`, `type`, `sources` on every page; content
pages (non-index/overview) must cite ≥1 source.

## Linking

- Cross-reference related pages with `[[Page Title]]` wikilinks. Link by the
  target page's **`title`** (Obsidian convention). The validator resolves a link
  if it matches a page's `title` OR its filename slug — but link by title for
  consistency. (This convention is pinned so the generator and `validate_wiki.py`
  never drift; a mismatch here is what the first proof run caught.)
- Every `[[link]]` must resolve to an existing page. If you reference a concept
  that has no page yet, create at least a stub page for it (title + frontmatter
  + one line) so the link resolves. **No dangling links.**
- Every content page should be reachable — linked from `index.md` or another
  page. No orphans.

## Contradiction handling (the heart of it)

When two sources disagree, **do NOT average them away or silently pick one.**
On the relevant page, add a section:

```markdown
## Open tensions
- **<one-line tension>**: Source N says "<quote>"; Source M says "<quote>".
  (status: unresolved | conditional on <X>)
```

A factual discrepancy (e.g. two different numbers for the same event) must be
flagged with both values and their sources — never collapsed to one or averaged.

## Required navigation pages

- `index.md` (type: index) — catalog of all pages, grouped by type.
- `overview.md` (type: overview) — 1-paragraph orientation to the whole wiki.

## Never confabulate

Only write claims supported by a cited source. If sources don't say it, it
doesn't go in the wiki. A rhetorical/strawman framing in a source is not a claim
the wiki should assert. Vendor/marketing claims — star counts, performance superlatives, self-reported adoption stats — must be framed as attributed claims ("X reports…", "according to [N]"), not stated as fact in the wiki's own voice.
