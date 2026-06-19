# Wiki-Weaver Evolution Plan

> **LIVING DOCUMENT** — last negotiated 2026-06-13. This captures decisions we have
> already made and is meant to be redlined. Sections marked **DRAFT — redline me** are
> explicitly open for the user to change. Everything else is established; if you want to
> change it, edit it here first, then we execute against it.

---

## 1. Context & Goal

**What wiki-weaver is.** A *code-first* LLM-wiki synthesis pipeline. It turns a pile of
source articles into a connected, deduplicated, provenance-tracked, error-free wiki —
the Karpathy "LLM Wiki" / second-brain pattern: *compile* knowledge into a persistent,
compounding corpus, don't re-derive it per query.

**The architecture (proven).** The attractor `PipelineEngine` runs an inner convergence
loop per source:

```
start → ingest → validate → assess → check → (feedback → loop_restart →) … → done
```

The dividing line is deliberate and load-bearing:

| Owned by CODE (deterministic, fail-loud) | Owned by AGENTS (LLM judgment) |
|---|---|
| Control flow / routing (DOT edges) | Synthesis — weave sources into pages |
| Structural validation (`validate_wiki.py`) | Convergence assessment (`assess`) |
| Dedup (content-hash source registry) | Refinement guidance (`feedback`) |
| Provenance / ledger (written only on real convergence) | |

This split is *why* the pipeline is dependable: an agent **cannot** fake "converged" —
code writes the ledger and archives the source only on a real pass.

**What is PROVEN today.**
- **Synthesis-on-write** — ingest weaves *by theme*, not by appending per-source
  sections. Source-labeled sections went **60 → 0** on proof clusters and held at corpus
  scale (0 across 100+ pages).
- **A recalibrated synthesis-quality eval** that cleanly **separates known-bad
  concatenation from known-good synthesis** — it FAILs the concatenation baseline
  (integration 2.75) and PASSes the woven proof clusters (4.12 / 4.25), including a
  held-out topic (anti-overfit).

**The through-line (from 10 parallel investigations).** Independently, the agents
converged on one conclusion: **we built a strong WRITER and a non-existent READER.** We
have two-thirds of Karpathy's triad (Ingest + Lint); **Query is a literal stub.** The
whole thesis of the pattern — "the wiki replaces RAG by being read directly" — is
currently *unproven* because there's nothing to read it. The goal of this plan is to
build the reader half and make wiki-weaver a **dependable, reusable second brain**, with
each step *proven* the way we proved synthesis.

---

## 2. The Eval-Driven Loop (the meta-pattern)

Every change in this plan runs through the same loop. It is the discipline that makes
results trustworthy rather than asserted.

1. **Write the gate** — encode "what good looks like" as a grader.
2. **Calibrate to FAIL** — prove the gate FAILs the CURRENT / frozen state. A gate that
   doesn't catch the known gap is worthless; this is the acceptance test for the *eval
   itself*.
3. **Implement** the change.
4. **Prove FAIL→PASS** on a small, representative slice.
5. **Guard overfit** — confirm PASS holds on a *held-out* slice (different topic/shape).
6. **Only then** resume the full corpus as the scale proof.

**Quality is a GATE, lexicographically first, never traded.** Efficiency is optimized
*only among quality-passers*. A cheaper or faster run that drops quality is a *fake* win
— the gate exists precisely to reject it. Every efficiency comparison is two-step:
(1) does it pass the quality gate? if no, reject; (2) among passers, who's cheaper/faster?

Keep scenarios **few and high-signal**, not a sprawling suite.

---

## 3. Reference Assets We Have

These frozen states are the measuring sticks. Do not overwrite them — they are what
"before" means.

| Asset | Role |
|---|---|
| `runs/known-bad/wiki` | **Known-bad** concatenation baseline. The eval's calibration anchor — graders pin to its 60 source-siloed sections. |
| `runs/corpus/wiki` (FROZEN @144p) | Current pipeline output — the **"before"** for this plan's changes. |
| `runs/proof-claudecode-v2` | **Overlap** proof cluster (Claude-Code topic) — fast iteration, high merge pressure. |
| `runs/proof-rag-v2` | **Held-out** proof cluster (RAG topic) — anti-overfit check. |
| `eval/grade_wiki.py` | Synthesis grader: deterministic gates (source-labeled-section count, single-source ratio, weave-where-overlap) + LLM judge (integration, claim-framing). |

---

## 4. The 5-Item Plan

### Gate table

| # | Item | Gate / criteria | Calibrated to FAIL against | Proof |
|---|---|---|---|---|
| **2** | **overview.md as a synthesized map** | `overview.md` is a navigational map (themes → hub pages + orienting prose), NOT a per-source thread log. Grader **must stop skipping** overview/index pages. | current `runs/corpus/wiki/overview.md` (concatenated thread log) | overview grader FAIL→PASS on same corpus + `proof-rag-v2` held-out |
| **3** | **Deepen provenance** | Every `[N]` resolves to real **author + source URL + date** (not just a filename); citations carry it. | current `.sources.json` (`{id, filename, hash}` only) | provenance grader FAIL→PASS; spot-check `[8]` → real URL/author/date |
| **1** | **ask/query layer + answer-quality eval** | Given a question, the wiki answers **grounded + cited + correct**, and **fails loud when the answer is absent** (no confabulation). **Better / more trustworthy than naive RAG** (synthesis + reliable refusal); cost/latency **comparable at this scale** (cheaper-at-scale untested). | a query harness over the current wiki (stub returns filename greps → FAILs grounded/cited/correct) | answer-quality eval FAIL→PASS; A/B vs RAG: **wiki-wins 6, raw-wins 0, tie 2, contested 1, never lost** (see "A/B results" below); absent-answer → loud "not in corpus" |
| **4** | **Consolidation / holistic re-weave** | A periodic whole-corpus pass re-weaves hub pages across *all* sources and **guarantees no claim loss** (completeness guard). | current per-source-only healing (never re-weaves the whole corpus) | consolidation eval: hub integration ↑, completeness = 0 dropped claims, FAIL→PASS |
| **5** | **Schema externalization** | Schema/validator/prompts are **project-supplied policy**, not hardcoded. Mechanism (engine) / policy (schema) split. | current hardcoded `SCHEMA.md` + validator + prompts | run an unmodified pipeline on a **2nd corpus** with a different supplied schema; both converge clean |

### Phase sequencing (A → E) and dependency rationale

- **Phase A — fixable-now (Items 2 + 3).** Cheap, independent of architecture. **Item 3
  is a hard prerequisite for Item 1** — the `ask` layer must cite real author/url/date,
  so provenance must deepen first.
- **Phase B — HEADLINE / architectural (Item 1).** The reader half. This is where the
  project's entire thesis ("replaces RAG by being read directly") gets *proven* instead
  of asserted. Depends on Phase A (real provenance to cite; a synthesized overview to
  navigate).
- **Phase C — architectural (Item 4).** Holistic re-weave + completeness guard. Benefits
  from the query layer (Item 1) to evaluate whether consolidation actually improves
  answerability.
- **Phase D — reusability (Item 5).** Mechanism/policy split, **proven on a 2nd corpus**.
  Sequenced late so we externalize a schema we already trust.
- **Phase E — scale proof.** Resume the full **748-article** corpus on the upgraded
  pipeline; grade *everything* against the frozen before-state (`runs/corpus/wiki`).

**Item 6 (acquisition front-end) is PARKED.** It may not belong in
wiki-weaver at all — likely lives in a separate acquisition tool as post-sync/download
processing that takes a dependency on this project. Revisit after Phase B.

---

## 5. CI Efficiency / Reliability Eval Axis

**Context-intelligence is ALREADY captured.** The CI hook is composed onto every
AmplifierSession (coordinator + every spawned node) and inherited by children. Layer-1
`events.jsonl` is written **regardless of whether a CI server is running** —
server-optional, fail-soft. (Confirmed on disk: the corpus run produced 456 session
dirs each with `context-intelligence/events.jsonl`, linked by 311 `session:fork` edges.)

**Where the value is: EVAL-side, not hot-path.** The thing one might *hope* for —
bespoke event-reading tooling that saves model calls *in the running pipeline* — is
honestly NOT here, because wiki-weaver already pushed that determinism into code+files.
What CI unlocks is an **efficiency/reliability eval axis** that `grade_wiki.py` is
structurally blind to, deterministically and nearly for free.

**Deliverable:** a read-only `eval/event_metrics.py` that joins **article → session**
via the ledger (`logs_dir → .runs/<ts>/<node>/status.json → session_id → events dir`)
and emits, per article and as a run rollup:

- cycles, tokens, **`cost_usd`** (actual, from `llm:response.usage` — not estimated),
- pages-read, delegation hops,
- node durations / failure reasons.

**Built ALONGSIDE Item 1** — it supplied the real-event cost/latency evidence for the
A/B vs RAG. Result (see "A/B results" below): the wiki is **better / more trustworthy**,
but **not cheaper** at this corpus scale (mean $0.55 vs $0.38, ~1.43x higher; latency
tied). The "cheaper than RAG" thesis is **OPEN** — revisit only at much larger corpus
scale and against an embedding-RAG baseline.

**The one capture gap (optional).** The *outer corpus sweep* is a plain code loop with
no AmplifierSession, so it doesn't appear in the event graph. Optionally wrap it in a CI
session (the pattern from `microsoft/amplifier-resolve`) to get the whole-run story.
Observability only.

**Explicit non-goals:** do **not** build hot-path event-reading into the running
pipeline; do **not** make anything depend on the CI *server* being up.

---

## A/B results (2026-06-13)

Phase B closed with a rigorous A/B holding **synthesis as the only variable**: the *same*
`ask` agent pointed at the synthesized wiki vs. at a raw source article directory (748
files). 9 scenarios (3 single-page, 3 cross-source, 3 absent), blinded comparison agent,
2-trial order-swap; cost/latency from real CI events on both sides.

**Quality thesis: PROVEN. Cost thesis: REFUTED at this scale.**

**Quality — the wiki never lost.**

```
wiki-wins=6  raw-wins=0  tie=2 (both correctly refused)  contested=1
```

Decisive on the two things synthesis is meant to buy:
- **Cross-source synthesis.** S1 surfaced the MCP-vs-CLI tension as *unresolved* (matching
  ground truth) where raw picked a side; S2 captured the single-turn-parallel-emission
  mechanism raw missed entirely.
- **Confabulation-resistance.** On the held-out **"Sourcegraph Cody"** trap (genuinely
  absent, adjacent to an AI-coding-assistants page), **raw RAG fabricated** detailed Cody
  claims as if sourced; the **wiki correctly refused**. The index makes "not covered"
  *verifiable*; the raw pile invites invention.
- Even **single-page lookups** went to the wiki — on provenance/citation quality (cites
  source page + IDs; raw cites nothing). Phase A's provenance work paying off.

**Cost / latency — "cheaper than RAG" NOT supported here.**
- Wiki mean **$0.55** vs raw **$0.38** per query (**~1.43x HIGHER**); latency **TIED**
  (~60s each). The wiki costs more *because* it does more synthesis / emits richer cited
  answers — the same reason it wins quality. A real quality/cost tradeoff, not a defect.

**Caveats / not-yet-proven.**
- Baseline is **agentic RAG** (an agent greps/reads raw articles), **not** cheap
  embedding-RAG. Against embedding-RAG the wiki would cost *more* still, but win
  quality/refusal by even more.
- **"Cheaper at scale" is untested** — 748 articles via grep isn't the scaling wall where
  RAG blows up. The wiki's theoretical cost edge, if any, lives at much larger corpora.

**Reframed positioning.** wiki-weaver's proven value is **QUALITY + TRUSTWORTHINESS**
(synthesis, real provenance, reliable refusal), **not per-query cost**. The cost thesis
is **OPEN** — revisit only with a much larger corpus **and** an embedding-RAG baseline.

---

## 6. Optimization Objective Hierarchy

Quality = the gate (first, never traded). *Within* the quality-passing region, each
stage minimizes a different efficiency target.

### Per-stage objectives — **ACCEPTED 2026-06-13** (redlined & locked)

| Stage | Quality gate (1st, hard) | Then minimize | Tolerate | Why |
|---|---|---|---|---|
| **Ingestion** | synthesis eval + structural valid | **cost_usd** | wall-time | background, runs on *every* new source, unattended — cheap matters, latency doesn't |
| **Retrieval / ask** | answer-quality (grounded/cited/correct/**fail-loud**) | **wall-time / latency** | cost | interactive — a human is *blocked*. (cost_usd is measured here, but A/B showed the wiki is **dearer, not cheaper** than agentic RAG at this scale — its proven edge is quality/trust, not the user's bill.) |
| **Consolidation** *(PARKED)* | hub-integration + **completeness (no claim loss)** | **cost_usd** | wall-time | periodic, background, rare |

**Accepted calls (2026-06-13):**
- **Ingestion** — minimize cost, tolerate wall-time; **no hard cost ceiling** (the convergence gate + `max_cycles` already bound per-article spend).
- **Retrieval/ask** — latency is a **soft target (~30–60s), not a hard gate**: we measured ~26–110s/ask (mean ~60s) and have no proven latency-reduction lever yet, so a hard gate would just fail without offering a fix. Revisit if real users find it sluggish. Cost is tolerated (pennies/query).
- **Consolidation** — objective retained but **PARKED**: Phase C's hub-integration grader showed accretion fingerprints (source-labeled headers, redundancy) are zero corpus-wide, so no consolidation pass is built. Re-open only if the grader flags real accretion as the corpus grows.

### The 3 knobs (ship as Item-5 policy) — **ACCEPTED defaults**

- **Model tier per stage** — **default: all-sonnet** (proven safe/reliable); `haiku` for the feedback stage is a documented **opt-in** for cost-sensitive runs (the board-games reuse scenario uses it).
- **Parallelism** — **reserved = 1** (within-wiki concurrency races on shared hub pages / `.sources.json` / ledger — proven unsafe; cross-wiki batching is a future sweep-level concern).
- **`max_cycles` budget** — **default: 3** (general balance); overridable per-run (the corpus run uses `5`; tiny wikis use `2`).

### Two evals this creates

1. **Stage-tradeoff A/B** — hold quality gate fixed, then compare cost/time across config
   profiles (e.g. cheap-model-ingest vs strong-model-ingest).
2. **Knob-responsiveness (dogfood / meta-eval)** — turning a knob moves its metric
   *monotonically* while quality holds. Proves the levers are real and gives others a
   tunable, dependable instance.

---

## 7. Strategies / Principles Playbook (back-pocket, living)

A two-tier approach: stock the **generalized** principles now; research **situational**
ones at problem-time, when concrete context sharpens the choice.

### The meta-lens

**(a) Mechanisms > instructions.** When you want a behavior, ask *"what structural change
forces it?"* before *"what prose asks for it?"* Structural moves cost zero marginal
context, can't be disobeyed, and don't decay. Examples:
- Strip a session's tools to `todo` + `delegate` → it *must* delegate (can't read/write/git),
  zero extra instruction.
- Code writes the ledger **only on real convergence** → an agent can't fake "converged."
- DOT **FAIL-edge** routes a retry → no "please retry" prose needed.
- **Focused-slice** starves context → forces cheap + scalable reads.

**(b) Every lever has an inverse — tune to the sweet spot, don't push the direction.**
- Decompose tasks → cheaper/sharper per step, but **too far** multiplies per-call
  overhead and loses cross-cutting synthesis.
- Move work LLM → code → free/fast/deterministic, but **too far** is brittle / overfit;
  keep the LLM for genuine judgment.
- Cheaper model → lower unit cost, but may need **more cycles** (net costlier) or drop
  quality.

**(c) Quality-gate-first** — never compare efficiency across runs that don't hold quality
fixed.

**(d) Measure-then-tune** — change one knob, read the metric, keep only proven wins.

**(e) Two-tier** — generalized principles stocked now; situational tactics researched at
problem-time.

### Cost / latency lever table

| Lever (mostly cost) | Buys | Inverse to watch | Mechanism invoked |
|---|---|---|---|
| Cheaper/smaller model per stage | $ down | may need more cycles (net costlier) or lower quality | model tier |
| Less / tighter context | $ + sharper attention | too little → misses connections, weaker synthesis | focused-slice |
| Decompose into focused tasks | $ + quality per step | too far → per-call overhead + lost cross-cutting synthesis | task granularity |
| Move work LLM → code | free, fast, deterministic | too far → brittle / overfit; loses versatility | mechanism-over-agent |
| Delegate to sub-sessions | frees parent context | needs forcing; naive prose is weak | tool-stripping forces it |
| Parallelism | wall-time down | cost unchanged; coordination overhead | concurrency |

---

## 8. External Feedback Triage

Another session used wiki-weaver's code on its own problem space (meeting transcripts,
captured under `~/dev/team-pulse-manager/...`). Read with discernment (the source is a
*different, struggling* project). Verdict: mostly confirmation, **2 genuine sparks**, **1
rejection**.

**Genuine sparks (adopt):**
1. **Cross-corpus validation as EVIDENCE.** Our core approach generalized to a *different
   domain* (meeting transcripts): it compounded, produced zero confabulation, **preserved
   a source error over its own training priors**, and self-flagged the unverifiable. This
   is real anti-overfit evidence — cite it as such; it strengthens the dependability
   claim.
2. **Claim-span provenance** as a bounded **Phase-2 of Item 3**: a claim → exact source
   *locator* (span), applied **only to load-bearing claims** (not every sentence — that
   way lies cost/fragility). Bounded scope is the whole point.

**Rejected (consciously):**
- Their **priority inversion** — they rank query/ask **LAST**. That's backwards from our
  evidence and reads as **failing-project bias** (they never got to the reader half).
  **Query/ask stays #1 (Phase B, the headline).** The inversion actually *reinforces* our
  ordering: the value is in being read.

---

## Appendix: Status at time of writing (2026-06-13)

- Engine fixes (spawn-outcome capture, AsyncClient lifecycle, fallback removal,
  packaging direct-ref) merged to `microsoft/amplifier-bundle-attractor` main.
- Synthesis pipeline + recalibrated eval published to
  `microsoft/amplifier-wiki-weaver` (private) + Microsoft-OSS compliance files added.
- Full 748 corpus run STOPPED at 144 converged pages → frozen as `runs/corpus/wiki`
  (resume in Phase E).
- Phase A is the next execution step: graders first, calibrated to FAIL the frozen corpus.
