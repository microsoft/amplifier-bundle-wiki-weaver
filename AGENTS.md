# AGENTS.md — wiki-weaver

Wiki-weaver compiles a structured, interlinked markdown wiki from source material and answers
questions by reading the compiled wiki instead of RAG (the Karpathy "LLM wiki" pattern).

## Commands (the five `.dot` pipelines)

Each user command is an attractor `.dot` pipeline in `pipeline/`, run via the CLI / lib:

- `init` (`init.dot`) — scaffold a wiki + LLM-design a domain-fit schema from `--purpose`.
- `ingest` (`ingest.dot` → invokes `synthesize.dot`) — drain `_inbox/` into the wiki.
- `ask` (`ask.dot`) — answer a question by reading the compiled wiki, with citations.
- `lint` (`lint.dot`) — deterministic structural validation (no LLM).

`doctor` (env diagnostics) and `query` (a naive substring-grep stub — not the query surface;
use `ask`) round out the CLI.

## Architecture

Commands are **thin lib wrappers over attractor `.dot` pipelines**:

- `cli/wiki_weaver.py` — argparse front end (dispatch only).
- `cli/lib.py` — importable concept-level functions; owns the outer corpus sweep and all
  process state (the `.processed.jsonl` ledger + `_archive/`), written by code *only* on real
  convergence (a deterministic tamper guard reverts agent-written process state and fails loud).
- `cli/engine_runner.py` — runs the inner pipelines on the attractor engine. The `.dot` files
  are `$token` templates; `build_*_from_file()` fills them with concrete paths/prompts before
  execution. The `.dot` files are **not** drop-in standalone.
- `cli/policy.py` — resolves per-wiki schema/rubric/model overrides (`<wiki>/policy/…`).

Runtime: requires the Amplifier runtime (`amplifier_foundation` + `unified_llm`); `pyproject`
deps are intentionally empty — **not** a standalone `pip install`. Run under the Amplifier
Python interpreter. `python -m wiki_weaver doctor` verifies the runtime is importable.

## Build / test

- Tests live in `eval/` (`test_*.py`) alongside the eval harnesses.
- Run with a venv that has `pytest` + `pyyaml`:

  ```bash
  pytest eval/ -q
  ```

- Run quality checks (format, lint, types) before committing.

## Data discipline (important)

- **NEVER commit source corpora** (articles/transcripts) or built wikis. `runs/`, `wiki/`,
  `.ai/`, and `.amplifier/evaluation/` are gitignored — keep it that way.
- Eval / run outputs belong in `~/.amplifier/evaluation/wiki-weaver/<datetime>/`, never the repo.
- Scenario fixtures under `eval/` are **synthetic by design** — keep them generic. No real
  names, internal product names, personal paths, or real source content. If a scenario needs a
  "team" example, keep it generic (e.g. "a team-decisions wiki"), not a real team/product.
