# Wiki-Weaver — Demo Runbook & Proof

Compile a pile of source material into a **connected, organized, error-free,
provenance-tracked** markdown wiki — the Karpathy LLM-wiki / second-brain "compounding memory"
pattern, made real and repeatable. Agents do the judgment work (schema design, synthesis,
quality, remediation); code owns control, validation, archiving, and dedup.

## Demo it (any pile of articles → a wiki)

`wiki-weaver` runs under an Amplifier Python interpreter. Invoke it as `python -m wiki_weaver <command>`
from the repo root.

```bash
PY=/path/to/amplifier/bin/python3   # python3 from your Amplifier install
cd wiki-weaver

$PY -m wiki_weaver doctor                   # env preflight (all green)

# Create a wiki and design a domain-fit schema from your purpose (one LLM call).
$PY -m wiki_weaver init mywiki \
  --purpose "A research second-brain on distributed systems: answer 'which approach for X',
  compare trade-offs, and track how conclusions evolve as more sources arrive."

cp ~/some_articles/*.md mywiki/_inbox/                  # drop in source material
$PY -m wiki_weaver ingest --wiki mywiki --max-cycles 5          # weave it in (one source at a time)
$PY -m wiki_weaver ask "what are the trade-offs of X vs Y?" --wiki mywiki   # query the compiled wiki
$PY -m wiki_weaver lint --wiki mywiki                           # structural validation -> PASS (exit 0)
```

Prefer a generic, no-LLM scaffold? Use `$PY -m wiki_weaver init mywiki --plain` (free, instant) and
`ingest`/`ask`/`lint` the same way.

Each source is ingested, structurally validated, quality-assessed, and — only when it
genuinely converges — archived with a provenance entry. Re-running is idempotent
(already-ingested sources are deduped by content hash), so you can keep dropping new files
into `_inbox/` and re-running `ingest` over time.

## What's proven (properties, validated this development cycle)

> Note: the run directories that produced this evidence are local working artifacts
> (`runs/` is gitignored) — they don't ship in the repo. Reproduce them with the demo above.

| Property | How it shows up |
|---|---|
| **Domain-adaptive schema** | `init --purpose` designs page types that fit the stated outcomes (e.g. a tool-landscape purpose yields `tool`/`comparison` pages; a team-decisions purpose yields `decision`/`owner` pages), not a fixed template. |
| **Compounds correctly** | `sources:` accrue in place (`[1] → [1, 2, 4]`); pages grow rather than fork; **0 duplicate** concept pages on re-ingest. |
| **Surfaces contradictions** | when sources disagree, an `## Open Tensions` section records **both sides + provenance**, rather than averaging them. |
| **Error-free** | the structural validator (links, orphans, frontmatter, provenance) exits 0 on every convergence. |
| **Dependable (no lying)** | the ledger/`_archive/` are written by code *only on real convergence*; an agent cannot fake "converged" (a deterministic tamper guard reverts and fails loud). |
| **Self-heals** | a broken `[[link]]` is repaired by the validate → feedback → ingest loop, which re-converges. |
| **Robust routing** | a flaky/empty `assess` verdict routes to *refine* (more work), never a dead-end or a false "converged". |

## Architecture (code-first, agents where they earn their keep)

`ingest`(agent) → `validate`(**code**) → `assess`(agent) → `check`(**code**) → done | `feedback`(agent) → loop.
The validator's exact failures are plumbed into the feedback/refine instructions (via file),
so remediation targets the real problem. Each node is an isolated session with a thin,
focused context — ingest reads a *slice* (touched + broken/orphan pages), not the whole
corpus, so it **scales** as the wiki grows. Full design: `docs/`.

## Honest residuals (non-blocking)

- The `assess` LLM node occasionally returns its verdict as prose instead of strict JSON.
  This is **non-fatal** — `check` routes any non-`converged` verdict (incl. unset/prose)
  to `refine`, so it costs at most an extra cycle and never falsely converges.
- Default model is `claude-sonnet-4-6` (set in `cli/engine_runner.py`, overridable via
  `WIKI_WEAVER_MODEL`). Chosen by a model-swap eval (`eval/model_sweep.py`): it converged
  with zero flakes, faster and cheaper than Opus-class. Use a premium model via
  `WIKI_WEAVER_MODEL` for the richest synthesis.
- `query` is a naive substring-grep stub — use `ask` to query a wiki.
