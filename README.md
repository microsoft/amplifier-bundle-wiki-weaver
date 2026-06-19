# Wiki-Weaver

Compile a structured, interlinked **markdown wiki** from a pile of source material, feed it
more content over time, and **read answers from the compiled wiki instead of RAG**. This is
the Karpathy "LLM wiki" / second-brain *compounding memory* pattern, made real and repeatable.

Agents do the judgment work (schema design, synthesis, quality assessment, remediation); code
owns control flow, structural validation, archiving, and deduplication. Each source is folded
into the growing wiki one at a time, and is only archived once it **genuinely converges** —
structurally valid and quality-assessed — with a provenance entry recording where every claim
came from.

> ⚠️ **Experimental exploration.** This is experimental software shared openly. See
> [SUPPORT.md](SUPPORT.md) for the (lack of) support policy.

## The idea

Instead of retrieving raw chunks at query time (RAG), wiki-weaver does the synthesis *up front*,
once, and stores the result as a navigable wiki:

- **`init`** designs a domain-fit *schema* from a plain-English description of what the wiki is
  for — the page types, frontmatter, and conventions tailored to your outcomes.
- **`ingest`** folds source files into the wiki one at a time, weaving overlapping content
  together, accruing provenance, and surfacing contradictions rather than averaging them.
- **`ask`** answers questions by *reading the compiled wiki* — no embeddings, no chunk
  retrieval. It cites the pages it used and refuses loudly when the wiki doesn't cover a topic.
- **`lint`** structurally validates the wiki (links, orphans, frontmatter, provenance).

The compiled wiki is just a folder of interlinked markdown — portable, diffable, and readable
without any tooling.

## Quick start

`wiki-weaver` runs under an [Amplifier](https://github.com/microsoft/amplifier) Python
interpreter (see [Requirements](#requirements)). Invoke it as `python -m wiki_weaver <command>` from
the repo root (the `wiki-weaver` console script is equivalent if installed).

```bash
cd wiki-weaver

# 0. Preflight — verifies the runtime, provider key, and validator are all present.
python -m wiki_weaver doctor

# 1. Create a wiki and design a schema for YOUR purpose (one LLM call, ~30–60s).
#    The --purpose text should describe the intended use and the outcomes you want.
python -m wiki_weaver init mywiki \
  --purpose "A personal research second-brain on distributed systems: answer 'which
  approach fits problem X', compare trade-offs between techniques, and track how my
  conclusions evolve as I read more."

# 2. Drop source material (markdown/plain text) into the wiki's inbox.
cp ~/notes/*.md mywiki/_inbox/

# 3. Weave the sources in. Re-run any time you add more to _inbox/ — it picks up where
#    it left off (already-ingested sources are deduped by content hash).
python -m wiki_weaver ingest --wiki mywiki --max-cycles 5

# 4. Ask questions — answered by reading the compiled wiki, with citations.
python -m wiki_weaver ask "what are the trade-offs between leader-based and leaderless replication?" \
  --wiki mywiki

# 5. Structurally validate the wiki (exit 0 = PASS).
python -m wiki_weaver lint --wiki mywiki
```

Prefer a generic, no-LLM scaffold? `python -m wiki_weaver init mywiki --plain` skips schema design and
uses the built-in generic schema (free, instant). You can always `ingest`/`ask`/`lint` the same
way afterward.

### Commands

| Command | What it does |
|---|---|
| `init <dir> --purpose "..."` | Scaffold a wiki and design a domain-fit schema (`<dir>/policy/schema.md`) from the stated purpose (and a sample of any staged `_inbox/` sources). `--plain` = generic scaffold, no LLM. `--no-sample-inbox` = design from `--purpose` alone. |
| `ingest --wiki <dir>` | Drain `<dir>/_inbox/` into the wiki, synthesizing/updating pages and archiving each source on convergence. Flags: `--source <file>` (one file), `--max-cycles N`, `--keep-going`. |
| `ask "<question>" --wiki <dir>` | Answer a question by reading the compiled wiki; cites pages used and refuses when the topic is absent. `--json` for structured output. |
| `lint --wiki <dir>` | Run the structural validator (links, orphans, frontmatter, provenance). Exit 0 = PASS. |
| `doctor [--wiki <dir>]` | Environment + (optional) wiki-structure diagnostics. |

> `query` exists but is a naive substring grep over page text — a minimal stub, **not** the
> query surface. Use `ask` to query a wiki.

## Three ways to use it

**1. CLI (primary surface).** The commands above, via `python -m wiki_weaver <command>` or the
`wiki-weaver` console script. This is the supported, end-to-end path.

**2. As a library.** The commands are thin wrappers over importable functions in
`cli.engine_runner`. They need the Amplifier runtime (see Requirements) — this is **not** a
standalone `pip install`.

```python
from wiki_weaver.engine_runner import run_init, run_ingest, run_ask, run_lint

run_init("mywiki", purpose="A research second-brain on distributed systems …")
# ... drop sources into mywiki/_inbox/ ...
run_ingest("mywiki", max_cycles=5)             # returns an InnerResult
result = run_ask("mywiki", "leader vs leaderless replication?")  # returns an AskResult
print(result.answer, result.pages_used, result.refused)
run_lint("mywiki")                             # returns an int exit code (0 = PASS)
```

(The richer per-source / keep-going ingest options exposed on the CLI live on
`cli.lib.ingest`; `run_ingest` runs the full inbox drain.)

**3. As `.dot` pipelines.** Each command is an attractor `.dot` pipeline under
[`pipeline/`](pipeline/): `init.dot`, `ingest.dot` (which invokes `synthesize.dot`),
`ask.dot`, and `lint.dot`. **These are not yet drop-in standalone.** They are `$token`
templates that the Python wrapper (`engine_runner.build_*_from_file()`) fills with concrete
paths/prompts before handing them to the engine. Today you run them through the CLI or library
above — they document the pipeline shape, not a portable artifact you can run by hand.

## Sharing / publishing your wiki

There is no `publish` command, by design — the compiled wiki **is** the deliverable: a folder
of interlinked markdown with a navigable `index.md` and `overview.md`. To share it:

- **Git:** commit the wiki directory to its own repo and push it.
- **Obsidian / any markdown editor:** open the wiki folder directly — `[[wikilinks]]` resolve.
- **Static site:** point a static-site generator (MkDocs, Hugo, etc.) at the folder.

(`_inbox/`, `_archive/`, `_failed/`, `.ai/`, `.runs/`, and the `.sources.json`/`.processed.jsonl`
bookkeeping files are operational state, not published content.)

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

## Configuration

| Env var | Purpose | Default |
|---|---|---|
| `WIKI_WEAVER_MODEL` | Model for the LLM pipeline/`init` nodes | `claude-sonnet-4-6` |
| `WIKI_WEAVER_PROVIDER` | Provider the LLM nodes route to | `anthropic` |

The default model was chosen by a model-swap eval (`eval/model_sweep.py`): it converged
reliably with no flakes, faster and cheaper than Opus-class. Use a premium model for the
richest synthesis via `WIKI_WEAVER_MODEL`.

## Requirements

- Python 3.11+
- An [Amplifier](https://github.com/microsoft/amplifier) installation — the pipeline runs on
  the attractor engine and routes to a configured LLM provider. Run wiki-weaver under the
  Amplifier Python interpreter (e.g. `~/.local/share/uv/tools/amplifier/bin/python3`); the
  `amplifier_foundation` and `unified_llm` packages are supplied by that runtime, **not** by
  `pip install`. `python -m wiki_weaver doctor` verifies they're importable.
- `ANTHROPIC_API_KEY` (or the relevant provider key) set in the environment.

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
