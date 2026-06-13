# Wiki-Weaver

A **code-first** pipeline that turns a pile of raw source articles into a **connected,
organized, error-free, provenance-tracked** wiki — the Karpathy "LLM wiki" / second-brain
*compounding memory* concept, made real and repeatable.

Agents do the judgment work (synthesis, quality assessment, remediation); code owns
control flow, structural validation, archiving, and deduplication. Each source is folded
into the growing wiki one at a time, and is only archived once it **genuinely converges** —
structurally valid and quality-assessed — with a provenance entry recording where every
claim came from.

> ⚠️ **Experimental exploration.** This is experimental software shared openly. See
> [SUPPORT.md](SUPPORT.md) for the (lack of) support policy.

## What it does

Drop a folder of markdown source articles into a wiki's `_inbox/`, and the pipeline:

- **Synthesizes** each source into thematically-organized concept pages — content from
  multiple sources is woven together *within* sections (not stacked source-by-source).
- **Compounds** knowledge in place: a concept page accrues sources (`sources: [1] → [1, 2, 4]`)
  as related articles arrive, with **zero duplicate** concept pages.
- **Surfaces contradictions** rather than averaging them — when sources disagree, an
  `## Open Tensions` section records both sides with provenance.
- **Frames claims honestly** — vendor/marketing assertions are attributed as claims, not
  asserted as established fact.
- **Validates structurally** on every cycle (links, orphans, frontmatter, provenance) and
  **self-heals** via a validate → feedback → ingest loop.
- **Never lies about convergence** — the ledger and archive are written by code *only* on
  real convergence; an agent cannot fake a "converged" status.

## Quick start

```bash
cd wiki-weaver

python -m cli doctor                          # environment preflight (all green)
python -m cli init   runs/demo/wiki           # create a new empty wiki
cp ~/some_articles/*.md runs/demo/wiki/_inbox/  # drop in source material
python -m cli ingest --wiki runs/demo/wiki --max-cycles 5   # weave it in
python -m cli lint   runs/demo/wiki           # structural validation -> PASS (exit 0)
```

Re-running is idempotent — already-ingested sources are deduplicated by content hash.

## Architecture

Code-first, with agents where they earn their keep:

```
ingest (agent) → validate (code) → assess (agent) → check (code) → done
                                        │
                                        └── feedback (agent) → loop
```

The validator's exact failures are plumbed into the feedback/refine instructions, so
remediation targets the real problem. Each node runs as an isolated session with a thin,
focused context — `ingest` reads only a *slice* (the touched page plus any broken/orphan
pages), not the whole corpus, so the pipeline **scales** as the wiki grows. The pipeline is
driven by the [attractor engine](https://github.com/microsoft/amplifier-bundle-attractor)
(a DOT-graph pipeline orchestrator).

Full design: [`docs/`](docs/).

## Synthesis-quality evaluation

`eval/grade_wiki.py` measures synthesis quality as a first-class, gated property — not just
structural validity. It combines deterministic metrics (source-labeled-section count,
single-source ratio, redundancy) with an optional LLM judge that scores **integration**
(is overlapping content merged where overlap exists?) and **claim-framing**. The grader is
calibrated against a known-bad baseline so it provably separates accumulated wikis from
genuinely synthesized ones.

```bash
python eval/grade_wiki.py synthesis runs/demo/wiki            # deterministic gate (fast)
python eval/grade_wiki.py synthesis runs/demo/wiki --judge    # + LLM integration/framing scores
```

## Configuration

| Env var | Purpose | Default |
|---|---|---|
| `WIKI_WEAVER_MODEL` | Model for the LLM pipeline nodes | `claude-sonnet-4-6` |
| `WIKI_WEAVER_PROVIDER` | Provider the inner nodes route to | `anthropic` |

The default model was chosen by a model-swap eval (`eval/model_sweep.py`): it converged
reliably with no flakes, faster and cheaper than Opus-class. Use a premium model for the
richest synthesis via `WIKI_WEAVER_MODEL`.

## Requirements

- Python 3.11+
- An [Amplifier](https://github.com/microsoft/amplifier) installation (the pipeline runs on
  the attractor engine and routes to a configured LLM provider)
- `ANTHROPIC_API_KEY` (or the relevant provider key) set in the environment

## Contributing

> [!NOTE]
> This project is not currently accepting external contributions, but we're actively working toward opening this up. We value community input and look forward to collaborating in the future. For now, feel free to fork and experiment!

Most contributions require you to agree to a
Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us
the rights to use your contribution. For details, visit [Contributor License Agreements](https://cla.opensource.microsoft.com).

When you submit a pull request, a CLA bot will automatically determine whether you need to provide
a CLA and decorate the PR appropriately (e.g., status check, comment). Simply follow the instructions
provided by the bot. You will only need to do this once across all repos using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or
contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft
trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship.
Any use of third-party trademarks or logos are subject to those third-party's policies.
