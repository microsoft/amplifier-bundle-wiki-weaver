# Phase D — Schema Externalization (mechanism / policy split)

> **Implementation spec for `modular-builder`.** Status: ARCHITECT-approved, ready to build.
> Goal: make wiki-weaver run a **second, differently-shaped wiki** (different domain,
> different page taxonomy) on the **same engine code** with only **project-supplied policy
> files** — and keep the existing `runs/corpus` wiki working with **zero** config.
>
> Ruthless-simplicity contract: the smallest seam that earns reusability. No plugin
> framework. Just project-supplied files at known paths under the wiki, with built-in
> fallback. Every knob justified below; the unjustifiable one (parallelism) is named and
> bounded, not silently shipped.

---

## 1. The key finding (why this is small)

The policy seam **already half-exists**. `engine_runner.build_dot()` (`cli/engine_runner.py:150`)
already injects the schema and rubric into the DOT by **file-path substitution**:

```python
"$schema_path": str(SCHEMA_PATH),                 # engine_runner.py:180  (const :39)
"$convergence_rubric": str(CONVERGENCE_RUBRIC_PATH),  # :183  (const :49)
```

Those targets are module-level constants pointing at `pipeline/`. Phase D does **not** invent
a mechanism — it makes those paths (plus the validator's config sets and the per-stage model)
**resolve from the project, falling back to the built-in defaults.** The DOT node wiring,
edges, `loop_restart`, `goal_gate`, fail-loud routing, dedup registry, tamper guard, and
ledger — all stay fixed engine machinery.

---

## 2. Policy vs Mechanism inventory (grounded)

| Asset (file:line) | Classification | Rationale |
|---|---|---|
| `pipeline/SCHEMA.md` | **POLICY** | Page taxonomy (`type:` enum, frontmatter contract), linking convention, contradiction format, overview rules, provenance footer, confabulation rule — all domain-shaped. |
| `pipeline/CONVERGENCE_RUBRIC.md` | **POLICY** (domain-neutral default) | The C1–C5 quality bar. Generic enough to be the default; overridable when a domain needs a different bar. |
| `validate_wiki.py` constants `NAV_PAGES`/`REQUIRED_FM`/`META_TYPES` (`:33–36`) | **POLICY** | Which page slugs are nav roots, which frontmatter keys are required, which `type:`s need not cite a source — all depend on the wiki's taxonomy. |
| `validate_wiki.py` `validate()` body (`:56`) | **MECHANISM** | The structural checks themselves (link resolution, orphans, frontmatter presence, provenance) are generic. |
| Prompt STRINGS in `synthesize.dot` ingest/assess/feedback nodes | **MIXED — default is engine, override is advanced** | Synthesis language ("weave by theme", "one page per concept") is generic enough to ship as the default; the HARD PROHIBITION (process-state ownership), remediation-first, and focused-slice are mechanism and must NOT be removed. Treat the whole `.dot` as an engine default that a project MAY override wholesale (escape hatch), but the expected override surface is `schema.md`, not the prompts. |
| `synthesize.dot` node/edge graph, `loop_restart`, `goal_gate`, routing (`:79–120`) | **MECHANISM** | Attractor control flow. Fixed. |
| `engine_runner.py` `PROVIDER`/`MODEL`/`REASONING_EFFORT` (`:81–88`), uniform per-node injection (`:200–205`) | **POLICY (the model-tier knob)** | Today every LLM node gets the *same* model. Per-stage model is a real lever. |
| `--max-cycles` (`wiki_weaver.py:765`, default 3) | **POLICY (the cycles knob)** | Convergence budget. |
| Outer sweep loop (`wiki_weaver.cmd_ingest:404`), dedup registry (`_assign_source_id:251`), tamper guard (`_detect_and_undo_tamper:325`), ledger/archive | **MECHANISM** | Dependability backbone. Fixed. |

**The externalized surface is therefore exactly four files + a knobs file.** Everything
else stays engine.

---

## 3. The project-policy contract

Policy travels **with the wiki** (each wiki carries its own policy — discovered relative to
`wiki_dir`, which the engine already has at every call site). Discovery is **convention over
configuration**: fixed file names under `<wiki>/policy/`, plus one knobs file at the wiki root.

```
<wiki_dir>/
  wiki.config.yaml            # OPTIONAL — knobs (models, max_cycles, provider)
  policy/                     # OPTIONAL — project-supplied policy files
    schema.md                # overrides pipeline/SCHEMA.md
    convergence-rubric.md    # overrides pipeline/CONVERGENCE_RUBRIC.md
    validator.yaml           # overrides validate_wiki.py's NAV_PAGES/REQUIRED_FM/META_TYPES
    inner.dot                # ADVANCED escape hatch — overrides the prompt/node set
  _inbox/ _archive/ .ai/ index.md overview.md .sources.json .processed.jsonl   # unchanged
```

**Every path is optional.** Any file absent → the built-in default is used. A wiki with no
`policy/` dir and no `wiki.config.yaml` behaves **byte-identically to today** (the regression
guarantee, §7).

### 3.1 `wiki.config.yaml` (knobs) — all keys optional

```yaml
provider: anthropic               # default: env WIKI_WEAVER_PROVIDER or "anthropic"
models:
  default: claude-sonnet-4-6      # base for any stage not named below
  ingest:   claude-sonnet-4-6     # per-stage override (optional)
  assess:   claude-sonnet-4-6
  feedback: claude-haiku-4-5      # example: cheaper model where judgment is light
max_cycles: 3                     # default 3; CLI --max-cycles overrides this
parallelism: 1                    # RESERVED — honored as 1 only (see §5)
```

### 3.2 `policy/validator.yaml` (validator config) — all keys optional

```yaml
nav_pages:            [index, overview, readme, log]   # orphan-exempt slugs
required_frontmatter: [title, type, sources]           # keys every page must carry
meta_types:           [index, overview, log, meta]     # types exempt from "must cite a source"
```

Defaults = the current hardcoded values (`validate_wiki.py:33–36`). When the file is absent,
the validator uses those defaults unchanged.

### 3.3 `policy/schema.md`, `policy/convergence-rubric.md`, `policy/inner.dot`

Plain drop-in replacements for the `pipeline/` files of the same role. Same `$var`
substitution contract for `inner.dot` (it must still reference `$source_path`, `$wiki_dir`,
`$schema_path`, `$convergence_rubric`, `$validate_cmd`, `$validation_report`, `$max_cycles`,
`$source_id` — the engine substitutes them). Most projects override **only `schema.md`**.

---

## 4. Engine load-with-fallback

Add one new module: **`cli/policy.py`**. Ruthlessly simple — `Path.exists()` checks plus a
small YAML read. No registry, no plugins.

```python
# cli/policy.py
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path

# Built-in defaults (the engine's own pipeline/ assets).
_PIPELINE = Path(__file__).resolve().parent.parent / "pipeline"
_DEF_SCHEMA   = _PIPELINE / "SCHEMA.md"
_DEF_RUBRIC   = _PIPELINE / "CONVERGENCE_RUBRIC.md"
_DEF_INNERDOT = _PIPELINE / "synthesize.dot"
_DEF_MODEL    = os.environ.get("WIKI_WEAVER_MODEL", "claude-sonnet-4-6")
_DEF_PROVIDER = os.environ.get("WIKI_WEAVER_PROVIDER", "anthropic")

@dataclass(frozen=True)
class WikiPolicy:
    schema_path: Path
    convergence_rubric_path: Path
    inner_dot_path: Path
    validator_config_path: Path | None   # None => validator uses built-in defaults
    provider: str
    models: dict[str, str]               # stage -> model; "default" is the base
    max_cycles: int
    parallelism: int                     # honored as 1 (see §5)

    def model_for(self, stage: str) -> str:
        return self.models.get(stage) or self.models.get("default") or _DEF_MODEL


def load_policy(wiki_dir: Path, *, cli_max_cycles: int | None = None) -> WikiPolicy:
    wiki_dir = Path(wiki_dir).expanduser().resolve()
    pol = wiki_dir / "policy"

    def pick(name: str, default: Path) -> Path:
        cand = pol / name
        return cand if cand.is_file() else default

    cfg = _read_yaml(wiki_dir / "wiki.config.yaml")   # {} if absent/unreadable
    models = dict(cfg.get("models") or {})
    models.setdefault("default", _DEF_MODEL)

    validator_cfg = pol / "validator.yaml"
    return WikiPolicy(
        schema_path=pick("schema.md", _DEF_SCHEMA),
        convergence_rubric_path=pick("convergence-rubric.md", _DEF_RUBRIC),
        inner_dot_path=pick("inner.dot", _DEF_INNERDOT),
        validator_config_path=validator_cfg if validator_cfg.is_file() else None,
        provider=str(cfg.get("provider") or _DEF_PROVIDER),
        models=models,
        max_cycles=int(cli_max_cycles if cli_max_cycles is not None
                       else cfg.get("max_cycles", 3)),
        parallelism=int(cfg.get("parallelism", 1)),
    )
```

`_read_yaml` mirrors the existing tolerant reader pattern in `engine_runner.load_ci_config`
(`:116`): try `import yaml`; return `{}` on any failure; `encoding="utf-8"`.

### 4.1 Integration points (exact edits)

1. **`engine_runner.build_dot`** (`:150`): add a `policy: WikiPolicy` parameter. Replace the
   constant substitutions with `policy.schema_path` / `policy.convergence_rubric_path`; read
   the DOT from `policy.inner_dot_path` instead of the `INNER_DOT` constant; append
   `--config {policy.validator_config_path}` to `validate_cmd` when it is not None. In the
   per-node injection loop (`:200–205`), look up the model **per node**:
   `llm_model="{policy.model_for(nid)}"` and `llm_provider="{policy.provider}"`.
2. **`engine_runner.run_inner`** (`:880`): call `policy = load_policy(wiki_dir, cli_max_cycles=max_cycles)`
   once, thread it into `build_dot`. Use `policy.max_cycles` for the substitution. Keep the
   public signature stable (still accepts `max_cycles`); internally it becomes the CLI override
   passed to `load_policy`.
3. **`engine_runner` ask/rag paths** (`build_ask_dot:~600`, `build_rag_dot:981`): these LLM
   nodes also hardcode `PROVIDER`/`MODEL`. Make them accept the resolved `provider` +
   `model_for("ask")` from `load_policy(wiki_dir)` so the model-tier knob applies to retrieval
   too. (Stage name `"ask"`; falls back to `default`.)
4. **`wiki_weaver.cmd_lint`** (`:506`): build the validator argv with the optional config:
   `argv = [sys.executable, str(VALIDATE_PY), str(wiki)]`; if `(wiki/"policy"/"validator.yaml").is_file()`
   append `["--config", str(...)]`. (Lint must use the same validator config the pipeline does,
   or lint and the `validate` node disagree.)
5. **`wiki_weaver.cmd_ingest`** (`:377`): unchanged control flow. `--max-cycles` keeps its
   CLI default of 3 but is now an **override**: pass `args.max_cycles` through to `run_inner`
   only when the user set it; otherwise let `wiki.config.yaml` decide. Simplest correct rule:
   keep passing `args.max_cycles`, and have `argparse` default to `None` (not 3) so
   `load_policy` can fall back to config. Update `p_ingest.add_argument("--max-cycles", type=int, default=None)`
   (`:765`).
6. **`validate_wiki.py`** (`:163` main): add `--config PATH`. When given, load the YAML and
   override the three module sets for that run; when absent, use the current hardcoded
   defaults. Refactor `validate()` to take an optional `config: dict | None` (defaulting to
   the built-ins) rather than reading globals — keeps it a pure function and testable.

No other files change. No engine (`amplifier-bundle-attractor`) change.

---

## 5. The three knobs — honest scope

| Knob | Where | Status | Note |
|---|---|---|---|
| **Model tier per stage** | `wiki.config.yaml: models.{ingest,assess,feedback,ask,default}` | **Implemented** | Resolved by `WikiPolicy.model_for(stage)`, injected per-node in `build_dot`. Real lever (cheap model for `feedback`, stronger for `assess`). **Model values are now FAMILY TOKENS (`sonnet`/`opus`/`haiku`) resolved live, not pinned ids — see §5.5.** |
| **`max_cycles`** | `wiki.config.yaml: max_cycles` (+ CLI override) | **Implemented** | Already a substitution var; just sourced from policy. |
| **Parallelism** | `wiki.config.yaml: parallelism` | **RESERVED — honored as 1** | **Decision: not a per-wiki runtime knob.** The outer sweep mutates shared wiki pages, `.sources.json`, the ledger, and `_archive/`; two sources concurrently editing the same hub page (e.g. `claude-code.md`) is a write race that corrupts synthesis and the tamper guard's snapshot. Within one wiki, ingest **must** stay sequential for correctness. The key is accepted into the schema for forward-compat and surfaced in `doctor`, but the executor honors `1`. If cross-wiki batching is wanted later, that's a *sweep-level* concern (independent wikis in parallel), not policy in a single wiki — out of scope for Phase D. |

This is the disciplined answer to "fold in all 3": two are real and wired; the third is named,
schema-reserved, and bounded with the exact reason it can't be a within-wiki knob — rather than
shipped as a config key that silently does nothing.

(The strawman per-stage objectives in `evolution-plan.md §6` remain **DRAFT/unredlined**.
Defaults above are fine; do not block Phase D on the redline.)

---

## 5.5 Model selection — live family resolution (SHIPPED)

> **Status: SHIPPED** to `microsoft/amplifier-app-wiki-weaver` (originally PR #4; the resolver
> engine now delegates to the upstream cross-provider resolver via PR #6, which consumes
> `amplifier-bundle-attractor` PR #68 — see "Cross-provider family resolution" below). This section
> is the authoritative model-selection stance and **supersedes** the pinned `claude-sonnet-4-6` /
> `claude-haiku-4-5` examples that appear as illustrative ids in §3.1, §4, and §7 — those remain
> in the spec as historical Phase-D artifacts, not as the strategy.

The original Phase-D draft pinned concrete model ids per stage. Pinning has a maintenance
tax: when a vendor ships a newer model in a family (e.g. `opus-4-8` → `opus-4-10`), every pin
must be hand-bumped, and a stale pin can silently 404. **wiki-weaver no longer pins model ids.**

**What ships instead.** A model spec is now either a **family token** (`sonnet` / `opus` /
`haiku`) or an **explicit model id**:

- A **family token** resolves at runtime: query the provider's **live model list**, filter to
  that family (glob), and pick the **newest stable model the provider actually serves**
  (numeric/date-aware version sort; preview/experimental excluded). The list and the generation
  go through the **same** upstream `unified-llm-client` adapter — *the adapter that lists is the
  adapter that generates* — so a resolved id is generation-compatible by construction (no
  "id-seam" where the listed id 404s at generation).
- An **explicit id** (e.g. `claude-sonnet-4-6`) **passes through unchanged** — no network call
  (back-compat for anyone who wants a hard pin).
- **Fail-loud**: a family that resolves to zero served models, an unreachable list, or a missing
  API key raises a clear error. There is **no silent fallback to a stale hardcoded id**.
- **Cached per run**: each `(provider, family)` resolves at most once per process, so a long
  ingest loop pays one round-trip per family.

**Per-stage defaults are now families, not ids:** `default` (and thus `ingest`/`assess`) =
`sonnet` (workhorse); `feedback` = `haiku` (cheaper/faster where judgment is light). Overrides
via `wiki.config.yaml: models.*` and `WIKI_WEAVER_MODEL` accept family tokens or explicit ids.

**Where it lives:** `wiki_weaver/model_resolver.py` (`resolve_model(provider, spec)`) — now a
**thin shim** over the upstream `unified-llm-client` resolver (`resolve_latest_for`, shipped in
attractor PR #68); wired into the per-node injection in `engine_runner.build_dot` and the defaults
in `wiki_weaver/policy.py` (`_DEF_MODEL = "sonnet"`, `_DEF_FEEDBACK_MODEL = "haiku"`). Explicit-id
passthrough, the per-process cache, and the anthropic-only family guard are preserved across the
shim swap (wiki-weaver PR #6).

**Not to be confused with engine/dependency versioning.** Model-id selection (this section) is
**live family resolution**. The engine/foundation **dependency** versions are a separate concern
and stay **pinned + governed by a hardened `upgrade` command** — that strategy is unchanged.
One is "which LLM do we call" (live, zero-pin); the other is "which engine code do we run"
(pinned, deliberate upgrades).

### Planned (not shipped) — cross-provider family resolution

Family-token resolution is **anthropic-only today**; a family token on any other provider
fail-louds. A **planned** evolution (under design, **council pending**) replaces the direct
Anthropic `/v1/models` call with a routing-matrix-style approach: **`fnmatch` glob matching over
`Provider.list_models()`** (amplifier-core's existing provider interface) plus a numeric-aware
version sort, so `opus`/`sonnet`/`haiku`-style family selection works uniformly across **all**
providers (openai / chat-completions / github-copilot / gemini). This reuses the provider
abstraction instead of a per-vendor HTTP call. **Marked planned, not built** — do not assume it
in current behavior.

---

## 6. Backward compatibility (the hard requirement)

A wiki with **no `policy/` dir and no `wiki.config.yaml`** must produce behavior identical to
pre-Phase-D. `load_policy` returns the built-in default paths, the env/hardcoded model, and
`max_cycles=3`. `build_dot` reads the same `pipeline/synthesize.dot`, substitutes the
same `pipeline/SCHEMA.md` + `pipeline/CONVERGENCE_RUBRIC.md`, injects the same uniform model
(because every stage resolves to `default`), and emits no `--config`. The `validate_wiki.py`
defaults are unchanged. `runs/corpus` keeps working untouched.

---

## 7. Proof plan (eval-driven — the §2 loop)

### 7.1 Regression (default wiki unchanged) — must pass FIRST

- **`test_policy_fallback.py`**: `load_policy(<tmp wiki, no policy>)` returns
  `schema_path == pipeline/SCHEMA.md`, `convergence_rubric_path == pipeline/CONVERGENCE_RUBRIC.md`,
  `inner_dot_path == pipeline/synthesize.dot`, `validator_config_path is None`,
  `provider == "anthropic"`, `model_for("ingest") == "claude-sonnet-4-6"`, `max_cycles == 3`.
- **Golden-DOT test**: capture `build_dot(src, <wiki, no policy>, 3, source_id=1)` output as a
  committed golden BEFORE the refactor; assert byte-identical AFTER. This is the mechanical
  guarantee that default wikis see zero behavior change.
- **Validator-default test**: `validate(<fixture wiki>)` with no config == today's result dict
  (run on a small committed fixture, compare `checks`/`passed`).
- **Live regression**: one real `wiki-weaver ingest --source <one corpus article> --wiki <copy of corpus wiki>`
  with no policy → converges, `lint` PASS. (Single source; full corpus is Phase E.)

### 7.2 Reusability (second, differently-shaped wiki) — the headline

Build `eval/scenario-02-reuse/` (a DIFFERENT domain with a DIFFERENT taxonomy):

- **Domain: tabletop board games** (small, concrete, has entities + comparisons + a planted
  contradiction). 3 tiny source articles (`sources/`):
  - `catan.md`, `wingspan.md`, `engine-building-mechanics.md` (frontmatter carries
    `title`, `author`, `source`/`url` so provenance has real data).
  - **Planted contradiction**: two sources give different player-counts or playtime for the
    same game → must surface in `## Open tensions` (exercises C3 on a new domain).
- **`policy/schema.md`** — a DIFFERENT taxonomy: `type:` enum =
  `game | mechanic | designer | comparison | catalog | landing` (deliberately NOT
  `index`/`overview`), catalog page named `catalog.md`, landing page `landing.md`.
- **`policy/validator.yaml`** — REQUIRED because the nav/meta page names differ:
  ```yaml
  nav_pages:  [catalog, landing]
  meta_types: [catalog, landing]
  required_frontmatter: [title, type, sources]
  ```
  (This deliberately forces the validator-config seam — with the default validator, `catalog`/
  `landing` would be flagged as orphans/uncited. Proving the override is exercised, not bypassed.)
- **`wiki.config.yaml`** — `max_cycles: 2`, `models.default: claude-sonnet-4-6` (proves the
  knobs load), and `models.feedback: claude-haiku-4-5` (proves per-stage tiering wires through).
- **Convergence-rubric**: do NOT override — proves the default rubric is domain-neutral (it
  judges synthesis/merge/contradiction/confabulation/provenance, not a specific taxonomy).
- **`inner.dot`**: do NOT override — proves the engine's default prompts + a swapped `schema.md`
  are sufficient for a different domain (the strong claim: change policy FILES, not prompts).

**Pass criteria (all must hold, same engine commit as the corpus run, no engine edits between):**

1. `wiki-weaver init <reuse_wiki>`, stage the 3 sources, `wiki-weaver ingest --wiki <reuse_wiki>`
   → **3/3 converge**, archived, ledger written.
2. `wiki-weaver lint --wiki <reuse_wiki>` → **PASS** (using the project's `validator.yaml`;
   `catalog`/`landing` not flagged).
3. Pages use the **new taxonomy**: assert at least one page has `type: game` and one
   `type: mechanic`; assert `catalog.md` (`type: catalog`) and `landing.md` exist.
4. The planted contradiction is surfaced in an `## Open tensions` section citing both sides
   (not averaged) — same machinery, new domain.
5. **Side-by-side anti-trivial check**: in the SAME test session, `load_policy(runs/corpus/wiki)`
   resolves to built-in defaults AND `load_policy(<reuse_wiki>)` resolves to the project files —
   one engine, two policies.

**Calibrate-to-fail (eval honesty):** before the validator-config wiring lands, running the
reuse wiki through the **default** validator must FAIL on `catalog`/`landing` (orphans +
uncited). That failure proves the seam is real; the wiring flips it to PASS.

---

## 8. Out of scope / non-goals (Sam's guardrails)

- **No plugin framework, no entry-points, no registry.** Files at known paths + fallback. That's it.
- **No within-wiki parallel ingest** (§5) — correctness, not laziness.
- **No splitting the DOT prompts into mechanism/policy fragments.** The `.dot` is an engine
  default with a whole-file override escape hatch; decomposing prompt strings is fragile and
  unjustified — the `schema.md` seam carries domain variation for the expected cases.
- **No new config surface beyond the 4 policy files + 1 knobs file.** If a future need appears,
  add it then with a real reason.
- **No change to `amplifier-bundle-attractor`.** Pure wiki-weaver-side refactor.

---

## 9. Build order (for `modular-builder`)

1. `cli/policy.py` (`WikiPolicy` + `load_policy` + `_read_yaml`) — pure, unit-tested first.
2. Refactor `validate_wiki.py` to a pure `validate(wiki_dir, config=None)` + `--config` arg.
3. Thread `WikiPolicy` through `engine_runner.build_dot` / `run_inner` / ask+rag builders.
4. Wire `cmd_lint` (`--config`) and `cmd_ingest` (`--max-cycles` default → `None`); add a
   `doctor` line echoing resolved policy (paths + models + max_cycles + parallelism).
5. Regression tests (§7.1) incl. the golden-DOT capture — must be green before §7.2.
6. `eval/scenario-02-reuse/` fixtures + the reusability proof (§7.2), calibrate-to-fail first.

**Stop-and-report triggers** (do not guess): if the golden-DOT byte-identical check cannot be
made to hold for default wikis (e.g. an unavoidable formatting change in per-node injection),
surface it — backward-compat is the hard gate, and a near-identical DOT needs explicit sign-off.

---

_Spec location: `wiki-weaver/docs/designs/PHASE_D_SCHEMA_EXTERNALIZATION.md`. Companion to
`evolution-plan.md §4` (Item 5 / Phase D) and `PIPELINE_DESIGN.md`._
