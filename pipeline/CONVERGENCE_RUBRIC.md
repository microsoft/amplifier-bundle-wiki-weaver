# Per-Source Convergence Rubric (the pipeline `assess` gate)

This rubric governs the inner convergence loop's `assess` node ONLY. It judges a
single question per cycle:

> **Has THIS one source — the article just ingested ($source_id) — been well
> integrated into the wiki?**

It is deliberately NOT the eval scorecard. The end-to-end scenario grader
(`eval/scenario-01-llm-wiki/rubric.md`) grades the WHOLE finished corpus wiki
against ground truth — all 6 sources, all canonical entities, the A/B test. That
is the wrong bar for a per-source loop: a freshly-ingested single article can
never show "all 6 sources integrated", so grading it with the scenario rubric
votes `refine` forever and the source never converges. Keep the two rubrics
separate; do not cross-apply them.

**Scope rule (read this first):** Convergence is judged ONLY over the delta this
source contributed plus the pages it touched. Do **NOT** require other corpus
sources to be present, do NOT demand whole-corpus completeness, and do NOT
penalize the wiki for concepts THIS source never discussed.

---

## What `assess` must NOT re-litigate

Structural mechanics — link resolution, frontmatter presence, orphan pages,
source-id provenance fields — are already gated deterministically by the
`validate` node (`validate_wiki.py`). If the pipeline reached `assess`, Tier-1
structure already passed. Do not re-score broken links or missing frontmatter
here; trust the validator and focus on integration QUALITY.

---

## Dimensions (score each 1–5 for THIS source only)

| dim | what it measures | 5 = | 1 = |
|---|---|---|---|
| C1 synthesis | the source's content was merged & pre-digested into the relevant concept pages, not dumped verbatim or appended as a raw blob | claims from multiple sources woven together within thematic sections; a reader cannot tell which source contributed which claim except via inline `[N]` citations | source pasted/concatenated as a whole separate "source-summary" dump page (the 1-extreme; source-siloed sections within a shared page score 2 — see note below) |
| C2 merge-correctness | concepts THIS source shares with existing pages updated those pages in place; one page per concept | existing pages extended; no near-duplicate created | a parallel duplicate page for a concept that already had one |
| C3 contradiction-handling | where this source genuinely conflicts with an existing page (or itself), the conflict is surfaced in an `## Open tensions` section citing both sides | real conflicts surfaced with both values + sources | a genuine conflict averaged away or silently overwritten |
| C4 no-confabulation | every non-trivial claim traces to THIS source or an already-existing page; ungrounded statements are written as `> TODO-VERIFY:` blockquotes, not asserted | zero unsupported assertions; gaps marked TODO-VERIFY | facts invented, or a rhetorical/strawman framing asserted as fact |
| C5 provenance | pages carrying this source's claims cite `$source_id` in their frontmatter `sources:` | this source's id present where its claims landed | claims from this source with no `$source_id` attribution |

### Source-siloed sections block convergence — labeled OR unlabeled

Removing source-labeled headers is necessary but **NOT sufficient** for C1 ≥ 4. Both patterns block convergence:

- **Source-labeled headers** (`## Topic (from Source N)`, `## Source N: ...`) → C1 **2** — explicit accumulation.
- **Thematic headers with one-source-per-section prose** — proper `## Theme` headers but each section's claims trace to a single source, with citations from disjoint sources distributed across separate sections rather than fused within shared sentences/paragraphs → C1 **2–3** — structural accumulation without labels.

**Concrete tell (apply this before scoring C1):** scan the inline `[N]` markers section by section. If most thematic sections cite only one source throughout, the page is **accumulated, not synthesized** — send `refine` with instructions to weave; do NOT converge.

**C1 ≥ 4 requires genuine in-section weaving:** claims from different sources fused WITHIN shared sentences or adjacent sentences in the same paragraph — multiple distinct `[N]` markers from different sources inside a single sentence or paragraph, not merely within the same top-level section. Distributing sources across separate sections of the same page does NOT meet this bar.

**C1 = 5:** a reader cannot determine how many sources a section drew from without reading the `[N]` markers.

The 1-extreme is a whole separate source-summary dump PAGE. `## ...(Source N)` headers and one-source-per-section layout (labeled or unlabeled) are convergence-blocking and MUST trigger a `refine` cycle.

### How to treat `> TODO-VERIFY:` blockquotes
A `> TODO-VERIFY:` blockquote is the CORRECT, PREFERRED move when the agent
cannot ground a claim in this source or an existing page. It is **honest gap
marking, not a defect** — never score C4 down for using one. Speculation
asserted as fact is the failure; a TODO-VERIFY is the fix.

---

## Convergence gate

`converged` **iff** every dimension C1–C5 is ≥ 4/5 **for this source**.
Otherwise `refine` (write targeted feedback, loop). The loop is hard-bounded by
`max_cycles` (an LLM-judged loop MUST terminate — attractor principle); if the
cap is reached without convergence, the run does NOT silently pass.

Remember: a small, honest, well-merged contribution from ONE source — even if it
only touches two or three pages and leaves the rest of the corpus untouched — is
a CONVERGED source. Completeness of the whole wiki is the eval's job, not this
gate's.
