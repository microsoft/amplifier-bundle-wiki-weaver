#!/usr/bin/env python3
# pyright: reportMissingImports=false
"""Phase C eval harness — wiki-weaver init schema-design quality scenarios.

For each scenario in eval/init_scenarios.yaml (3 total: 2 regular + 1 held-out):
  1. Creates a fresh wiki dir under the run's output dir (<outdir>/<id>/wiki).
  2. Calls run_init(wiki_dir, purpose=<scenario.purpose>, sample_inbox=False, plain=False).
     NOTE: sample_inbox=False — judges design-from-purpose; does NOT read source_dir data.
  3. Reads the produced <wiki>/policy/schema.md (missing/empty = hard fail, score 0).
  4. Applies deterministic gates:
       - policy/schema.md exists and is non-empty
       - declares a page-type taxonomy (contains 'type:')
       - keeps index and overview as nav pages (mentions both)
       - run_init exit code == 0
  5. Grades with an LLM judge: scores each grader dimension 0-5, citing specifics from schema.
     Judge receives: purpose, outcomes_to_serve, anti_patterns, produced schema, generic default.
  6. Writes results.json + summary.md to ~/.amplifier/evaluation/wiki-weaver/<sortable-datetime>/.
     Per scenario: a copy of the produced schema.md lives at <outdir>/<id>/schema.md.

Verdict: weighted_mean >= 3.5 AND no dimension < 3 AND all deterministic gates pass = PASS.

Result status field: PASS | FAIL | ERROR | ?
  ERROR = infrastructure failure (run_init raised an exception).
  FAIL  = run_init returned non-zero, gates failed, score below threshold, or a dimension < 3.
  Only PASS/FAIL count for the suite verdict.

Usage:
    python eval/run_init_eval.py                        # all 3 scenarios
    python eval/run_init_eval.py --limit 1              # smoke: one scenario
    python eval/run_init_eval.py --list                 # list scenario IDs, no run
    python eval/run_init_eval.py --scenarios <file>     # different scenarios file
    python eval/run_init_eval.py --judge-model <m>      # different judge model
    python eval/run_init_eval.py --concurrency 2        # reduce parallelism

Judge pattern: mirrors run_ask_eval._build_judge_fn exactly — unified_llm.generate()
wrapped in asyncio.run() for sync compatibility. In this async harness the judge runs
in a thread via run_in_executor so asyncio.run() can safely create its own event loop.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml  # type: ignore[import-unresolved]

# ---------------------------------------------------------------------------
# sys.path: let eval/event_metrics and cli imports work regardless of cwd
# ---------------------------------------------------------------------------
_EVAL_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _EVAL_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from event_metrics import ask_run_metrics  # noqa: E402

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCENARIOS_FILE = _EVAL_DIR / "init_scenarios.yaml"
DEFAULT_JUDGE_MODEL = "claude-sonnet-4-6"
OUTPUT_ROOT = Path.home() / ".amplifier" / "evaluation" / "wiki-weaver"


# ---------------------------------------------------------------------------
# Load scenarios + meta
# ---------------------------------------------------------------------------


def load_scenarios_and_meta(path: Path = SCENARIOS_FILE) -> tuple[list[dict], dict]:
    """Load scenarios and meta from init_scenarios.yaml.

    Returns (scenarios, meta). Fails loudly if the file does not exist — no silent fallback.
    """
    if not path.exists():
        sys.exit(f"ERROR: scenarios file not found: {path}")
    raw = path.read_text(encoding="utf-8")
    doc = yaml.safe_load(raw)
    return doc.get("scenarios", []), doc.get("meta", {})


def load_scenarios(path: Path = SCENARIOS_FILE) -> list[dict]:
    """Load only scenarios list (compatibility alias used by pytest imports)."""
    scenarios, _ = load_scenarios_and_meta(path)
    return scenarios


# ---------------------------------------------------------------------------
# LLM judge — mirrors run_ask_eval._build_judge_fn exactly
# ---------------------------------------------------------------------------


def _build_judge_fn(model: str = DEFAULT_JUDGE_MODEL):
    """Mirror of run_ask_eval._build_judge_fn.

    Returns a sync callable judge_fn(prompt) -> str, or None if unified_llm
    is unavailable.  In this async harness the callable is invoked via
    run_in_executor so asyncio.run() inside it runs in its own thread event loop.
    """
    try:
        import asyncio as _asyncio  # noqa: PLC0415

        from unified_llm import generate  # type: ignore[import-unresolved]  # noqa: PLC0415

        def _judge(prompt: str) -> str:
            result = _asyncio.run(generate(model, prompt=prompt))
            return result.text

        return _judge
    except Exception as exc:  # noqa: BLE001
        print(
            f"WARN: unified_llm not importable ({exc}); judge unavailable.",
            file=sys.stderr,
        )
        return None


# ---------------------------------------------------------------------------
# Judge prompt builder — f-strings safe against { } in schema/purpose text
# ---------------------------------------------------------------------------


def _build_judge_prompt(
    scenario: dict,
    produced_schema: str,
    generic_schema: str,
    dimensions: list[dict],
) -> str:
    """Build the schema-quality judge prompt for a single init scenario.

    Uses f-strings (not .format()) so that { } characters inside
    produced_schema, generic_schema, or purpose text do not interfere.
    """
    purpose = scenario.get("purpose", "").strip()
    outcomes = scenario.get("outcomes_to_serve", [])
    anti = scenario.get("anti_patterns", [])

    outcomes_str = (
        "\n".join(f"  - {o}" for o in outcomes) if outcomes else "  (none listed)"
    )
    anti_str = "\n".join(f"  - {a}" for a in anti) if anti else "  (none listed)"

    # Dimension scoring instructions block
    dims_block = "\n".join(
        f"  {d['id']} (weight={d['weight']}): {d['question'].strip()}"
        for d in dimensions
    )

    # Return-format block: one entry per dimension
    dim_ids = [d["id"] for d in dimensions]
    return_format = (
        "{\n"
        + ",\n".join(
            f'  "{did}": {{"score": <0-5>, "reason": "<one line citing specifics from schema>"}}'
            for did in dim_ids
        )
        + "\n}"
    )

    return f"""You are evaluating whether a wiki schema is well-designed for its domain and purpose.
Return ONLY valid JSON — no explanation outside the JSON block.

Return format:
{return_format}

=== WIKI PURPOSE ===
{purpose}

=== DESIRED OUTCOMES THE SCHEMA MUST SERVE ===
{outcomes_str}

=== ANTI-PATTERNS TO PENALIZE ===
{anti_str}

=== PRODUCED SCHEMA (the schema under evaluation) ===
{produced_schema}

=== GENERIC DEFAULT SCHEMA (the domain-agnostic baseline — the schema to beat) ===
{generic_schema}

Scoring dimensions (0–5 each; 5=excellent, 4=good, 3=acceptable, 2=weak, 1=poor, 0=absent/harmful):
{dims_block}

Rules:
- For each dimension, cite SPECIFIC text from the produced schema to justify the score.
- Score 0 if the dimension is entirely absent or the schema actively fails the criterion.
- Score 5 if the schema clearly and specifically serves this criterion in ways the generic default does not.
- Comparing against the generic default is required for the 'differentiation' dimension.
- An answerable outcome NOT served by any structural element must lower outcome_orientation.
"""


# ---------------------------------------------------------------------------
# Schema scorer
# ---------------------------------------------------------------------------


def _score_schema(
    scenario: dict,
    schema_text: str,
    generic_schema: str,
    dimensions: list[dict],
    judge_fn,
) -> dict:
    """Score one produced schema with LLM judge.

    Returns dict mapping dimension_id -> {score, reason}.
    All dimensions are always present in the output (None score if unavailable).
    """
    empty = {
        d["id"]: {"score": None, "reason": "judge unavailable"} for d in dimensions
    }
    if judge_fn is None:
        return empty

    prompt = _build_judge_prompt(scenario, schema_text, generic_schema, dimensions)

    try:
        raw = judge_fn(prompt)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        parsed: dict = json.loads(m.group(0)) if m else {}
    except Exception as exc:  # noqa: BLE001
        return {
            d["id"]: {"score": None, "reason": f"judge error: {exc}"}
            for d in dimensions
        }

    # Normalise: ensure all dimensions are present with canonical shape
    result: dict = {}
    for d in dimensions:
        did = d["id"]
        val = parsed.get(did)
        if isinstance(val, dict):
            result[did] = {
                "score": val.get("score"),
                "reason": str(val.get("reason", "")),
            }
        elif isinstance(val, (int, float)):
            # Flat form: {"domain_fit": 4, ...} — accept it
            result[did] = {"score": val, "reason": ""}
        else:
            result[did] = {
                "score": None,
                "reason": f"missing or unexpected from judge: {val!r}",
            }

    return result


# ---------------------------------------------------------------------------
# Verdict computation
# ---------------------------------------------------------------------------


def _compute_verdict(
    gate_results: dict[str, bool],
    dim_scores: dict,
    dimensions: list[dict],
) -> tuple[bool | None, float | None]:
    """Compute PASS/FAIL verdict from gates + dimension scores.

    PASS = all gates pass AND weighted_mean >= 3.5 AND no dimension < 3.
    Returns (passed, weighted_mean).
      passed = None  → judge unavailable; cannot determine
      passed = False → gates failed OR score too low OR a dimension < 3
      passed = True  → all gates pass AND score threshold met
    """
    # Gates must all pass first (includes rc==0 check)
    if not all(gate_results.values()):
        return False, None

    # Compute weighted mean (skip None scores)
    total_weight = 0.0
    weighted_sum = 0.0
    for d in dimensions:
        did = d["id"]
        score_val = (
            dim_scores.get(did, {}).get("score")
            if isinstance(dim_scores.get(did), dict)
            else None
        )
        if score_val is None:
            continue
        w = float(d.get("weight", 1))
        weighted_sum += float(score_val) * w
        total_weight += w

    if total_weight == 0.0:
        return None, None  # no scores available

    weighted_mean = weighted_sum / total_weight

    # Floor check: no individual dimension < 3 (unweighted)
    for d in dimensions:
        did = d["id"]
        score_val = (
            dim_scores.get(did, {}).get("score")
            if isinstance(dim_scores.get(did), dict)
            else None
        )
        if score_val is not None and float(score_val) < 3:
            return False, weighted_mean

    return weighted_mean >= 3.5, weighted_mean


# ---------------------------------------------------------------------------
# Single scenario runner
# ---------------------------------------------------------------------------


async def _run_one(
    scenario: dict,
    out_dir: Path,
    meta: dict,
    judge_fn,
    sem: asyncio.Semaphore,
    generic_schema: str,
) -> dict:
    """Run init → deterministic gates → LLM judge. Returns full result dict.

    status field: "PASS" | "FAIL" | "ERROR" | "?"
    ERROR means an infrastructure exception (run_init raised); excluded from verdict.
    """
    async with sem:
        loop = asyncio.get_event_loop()
        sc_id = scenario["id"]
        purpose = (scenario.get("purpose") or "").strip()
        dimensions: list[dict] = meta.get("grader", {}).get("dimensions", [])

        # 1. Create a fresh wiki dir under this scenario's output subdir
        sc_out_dir = out_dir / sc_id
        sc_out_dir.mkdir(parents=True, exist_ok=True)
        wiki_dir = sc_out_dir / "wiki"
        wiki_dir.mkdir(parents=True, exist_ok=True)

        # 2. Call run_init in executor (it calls asyncio.run() internally — must be in thread)
        t_start = time.time()
        error_reason: str | None = None
        rc: int = -1

        def _do_init() -> int:
            # Import inside thread to avoid asyncio context conflicts
            from wiki_weaver.engine_runner import run_init  # noqa: PLC0415

            return run_init(
                wiki_dir,
                purpose=purpose if purpose else None,
                sample_inbox=False,
                plain=False,
            )

        try:
            rc = await loop.run_in_executor(None, _do_init)
        except Exception as exc:  # noqa: BLE001
            error_reason = f"run_init raised: {exc}"

        duration_s = round(time.time() - t_start, 2)

        # Infrastructure exception → ERROR; do not grade
        if error_reason is not None:
            return {
                "id": sc_id,
                "domain": scenario.get("domain", ""),
                "held_out": scenario.get("held_out", False),
                "purpose_excerpt": purpose[:200],
                "rc": rc,
                "gates": {
                    "schema_exists_nonempty": False,
                    "has_type_taxonomy": False,
                    "has_nav_pages": False,
                    "run_init_rc_zero": False,
                },
                "dimension_scores": {},
                "weighted_mean": None,
                "pass": None,
                "status": "ERROR",
                "error_reason": error_reason,
                "schema_path": str(sc_out_dir / "schema.md"),
                "cost_usd": 0.0,
                "duration_s": duration_s,
                "events_found": False,
            }

        # 3. Read produced schema.md (may be absent if rc != 0)
        schema_file = wiki_dir / "policy" / "schema.md"
        schema_text: str | None = None
        if schema_file.is_file():
            try:
                schema_text = schema_file.read_text(encoding="utf-8")
            except OSError:
                schema_text = None

        # Copy schema into scenario output dir for persistence regardless of verdict
        if schema_text is not None:
            try:
                (sc_out_dir / "schema.md").write_text(schema_text, encoding="utf-8")
            except OSError:
                pass

        # 4. Deterministic gates
        gate_results: dict[str, bool] = {
            "schema_exists_nonempty": bool(schema_text and schema_text.strip()),
            "has_type_taxonomy": bool(schema_text and "type:" in schema_text),
            "has_nav_pages": bool(
                schema_text
                and "index" in schema_text.lower()
                and "overview" in schema_text.lower()
            ),
            "run_init_rc_zero": rc == 0,
        }

        # 5. CI metrics from the most recent init-* logs dir
        metrics: dict = {"cost_usd": 0.0, "wall_time_s": None, "events_found": False}
        try:
            runs_dir = wiki_dir / ".runs"
            if runs_dir.is_dir():
                init_dirs = [
                    d
                    for d in runs_dir.iterdir()
                    if d.is_dir() and d.name.startswith("init-")
                ]
                if init_dirs:
                    latest_logs = max(init_dirs, key=lambda d: d.stat().st_mtime)
                    metrics = await loop.run_in_executor(
                        None, ask_run_metrics, wiki_dir, latest_logs
                    )
        except Exception:  # noqa: BLE001
            pass

        # 6. LLM judge (in executor: it calls asyncio.run() — needs own thread event loop)
        dim_scores: dict = {}
        if schema_text and judge_fn is not None:
            dim_scores = await loop.run_in_executor(
                None,
                _score_schema,
                scenario,
                schema_text,
                generic_schema,
                dimensions,
                judge_fn,
            )
        elif not schema_text:
            # No schema to judge; mark all dimensions as zero (file not written)
            dim_scores = {
                d["id"]: {"score": 0, "reason": "schema.md not produced by run_init"}
                for d in dimensions
            }

    # 7. Verdict (outside semaphore — pure computation)
    passed, weighted_mean = _compute_verdict(gate_results, dim_scores, dimensions)

    if passed is True:
        status = "PASS"
    elif passed is False:
        status = "FAIL"
    else:
        status = "?"

    return {
        "id": sc_id,
        "domain": scenario.get("domain", ""),
        "held_out": scenario.get("held_out", False),
        "purpose_excerpt": purpose[:200],
        "rc": rc,
        "gates": gate_results,
        "dimension_scores": dim_scores,
        "weighted_mean": round(weighted_mean, 3) if weighted_mean is not None else None,
        "pass": passed,
        "status": status,
        "error_reason": None,
        "schema_path": str(sc_out_dir / "schema.md"),
        "cost_usd": metrics.get("cost_usd", 0.0),
        "duration_s": duration_s,
        "events_found": bool(metrics.get("events_found", False)),
    }


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------


def _write_summary(results: list[dict], out_dir: Path, dimensions: list[dict]) -> None:
    """Write summary.md to out_dir."""
    total = len(results)
    n_passed = sum(1 for r in results if r.get("pass") is True)
    n_failed = sum(1 for r in results if r.get("pass") is False)
    n_error = sum(1 for r in results if r.get("status") == "ERROR")
    quality_total = total - n_error

    held_out = [r for r in results if r.get("held_out")]
    held_passed = sum(1 for r in held_out if r.get("pass") is True)

    total_cost = sum(r.get("cost_usd") or 0.0 for r in results)

    if n_error > 0 and quality_total == 0:
        suite_verdict = "INCOMPLETE"
    elif n_failed == 0 and quality_total > 0:
        suite_verdict = "PASS"
    else:
        suite_verdict = "FAIL"

    # Column headers: abbreviated dimension ids to match spec summary format
    _DIM_ABBR = {
        "domain_fit": "domain_fit",
        "outcome_orientation": "outcome",
        "differentiation": "diff",
        "validity": "validity",
    }
    dim_cols: list[str] = [
        str(_DIM_ABBR.get(str(d["id"]), str(d["id"]))) for d in dimensions
    ]

    lines: list[str] = [
        "# wiki-weaver init — Phase C Eval Summary",
        "",
        f"**Suite: {suite_verdict}** "
        f"({n_passed}/{quality_total} passed"
        + (f", {n_failed} failed" if n_failed else "")
        + (f", **{n_error} ERROR (infra)**" if n_error else "")
        + ")",
    ]
    if n_error > 0:
        lines += [
            "",
            "**⚠ INCOMPLETE — infra errors excluded from verdict. Re-run to get a valid result.**",
        ]
    lines += [
        "",
        f"**Held-out generalization signal: {held_passed}/{len(held_out)} passed**"
        " _(do not tune against these)_",
        "",
        "## Cost / Latency",
        "",
        f"- Total cost (all scenarios): **${total_cost:.4f}**",
        "",
        "## Per-Scenario Results",
        "",
        "| ID | Domain | H/O | STATUS | " + " | ".join(dim_cols) + " | mean | gates |",
        "|---|---|---|---|" + "---|" * len(dim_cols) + "---|---|",
    ]

    for r in sorted(results, key=lambda x: x["id"]):
        ho = "✓" if r.get("held_out") else ""
        status = r.get("status") or "?"
        ds = r.get("dimension_scores", {})
        gates_ok = all(r.get("gates", {}).values()) if r.get("gates") else False
        gate_mark = "✓" if gates_ok else "✗"
        mean_val = r.get("weighted_mean")
        mean_str = f"{mean_val:.2f}" if mean_val is not None else "-"

        dim_cells = " | ".join(
            str(
                ds.get(d["id"], {}).get("score", "-")
                if isinstance(ds.get(d["id"]), dict)
                else "-"
            )
            for d in dimensions
        )
        err_note = f" _{r.get('error_reason', '')[:60]}_" if status == "ERROR" else ""
        lines.append(
            f"| {r['id']} | {r.get('domain', '')} | {ho} | **{status}**{err_note} "
            f"| {dim_cells} | {mean_str} | {gate_mark} |"
        )

    lines += [
        "",
        "## Held-out Scenario Detail",
        "",
        "_(Held-out IDs are the real generalization signal — not tuned against.)_",
        "",
    ]
    for r in [x for x in results if x.get("held_out")]:
        ds = r.get("dimension_scores", {})
        verdict = r.get("status") or "?"
        lines += [
            f"### {r['id']} ({r.get('domain', '')}) — {verdict}",
            f"**Purpose:** {r.get('purpose_excerpt', '')}...",
            "",
            "**Dimension scores:**",
        ]
        for d in dimensions:
            did = d["id"]
            entry = ds.get(did)
            score = entry.get("score", "-") if isinstance(entry, dict) else "-"
            reason = entry.get("reason", "") if isinstance(entry, dict) else ""
            lines.append(f"  - `{did}` (w={d['weight']}) = {score}: {reason}")
        mean_val = r.get("weighted_mean")
        lines += [
            "",
            f"**Weighted mean:** {f'{mean_val:.3f}' if mean_val is not None else 'n/a'}",
            f"**Gates:** {r.get('gates', {})}",
            "",
        ]

    lines += [
        "## Notes",
        "",
        "- PASS threshold: weighted_mean ≥ 3.5 AND no dimension < 3 AND all deterministic gates pass",
        "- Dimension weights: "
        + ", ".join(f"{d['id']}={d['weight']}" for d in dimensions),
        "- `H/O` = held-out (generalization guard; do not tune init prompt against these)",
        "- ERROR = infrastructure exception in run_init; excluded from verdict",
        "- Grading: judge receives purpose, outcomes_to_serve, anti_patterns, produced schema, "
        "and generic default (pipeline/SCHEMA.md) for contrast",
    ]

    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Main eval runner
# ---------------------------------------------------------------------------


async def _run_eval(
    scenarios_path: Path,
    limit: int | None,
    judge_model: str,
    concurrency: int = 4,
) -> int:
    scenarios, meta = load_scenarios_and_meta(scenarios_path)
    if limit is not None:
        scenarios = scenarios[:limit]

    dimensions: list[dict] = meta.get("grader", {}).get("dimensions", [])

    # Load generic default schema (relative path in meta, resolved from repo root)
    generic_schema_rel = meta.get("generic_default_schema", "pipeline/SCHEMA.md")
    generic_schema_path = _REPO_ROOT / generic_schema_rel
    if not generic_schema_path.is_file():
        sys.exit(f"ERROR: generic_default_schema not found: {generic_schema_path}")
    generic_schema = generic_schema_path.read_text(encoding="utf-8")

    judge_fn = _build_judge_fn(judge_model)
    sem = asyncio.Semaphore(concurrency)

    # Create output dir — same sortable-datetime convention as run_ask_eval
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = OUTPUT_ROOT / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Running {len(scenarios)} scenario(s)  [init schema-design eval]")
    print(f"Judge model: {judge_model}  |  Concurrency: {concurrency}")
    print(f"Output:      {out_dir}")
    if judge_fn is None:
        print(
            "WARN: judge unavailable — dimension scores will be None, all verdicts will be ?"
        )
    print()

    tasks = [
        _run_one(sc, out_dir, meta, judge_fn, sem, generic_schema) for sc in scenarios
    ]
    results: list[dict] = list(await asyncio.gather(*tasks))

    # Write output
    (out_dir / "results.json").write_text(
        json.dumps(results, indent=2, default=str), encoding="utf-8"
    )
    _write_summary(results, out_dir, dimensions)

    # Console summary (mirrors run_ask_eval console output pattern)
    n_passed = sum(1 for r in results if r.get("pass") is True)
    n_failed = sum(1 for r in results if r.get("pass") is False)
    n_error = sum(1 for r in results if r.get("status") == "ERROR")
    quality_total = len(results) - n_error
    held = [r for r in results if r.get("held_out")]
    held_passed = sum(1 for r in held if r.get("pass") is True)
    total_cost = sum(r.get("cost_usd") or 0.0 for r in results)

    print(f"\nResults → {out_dir}")
    print(
        f"Suite: {n_passed}/{quality_total} passed"
        + (f", {n_failed} failed" if n_failed else "")
        + (f", {n_error} ERROR (infra)" if n_error else "")
        + f"  |  Held-out: {held_passed}/{len(held)}"
        + f"  |  Total cost: ${total_cost:.4f}"
    )
    print()
    for r in sorted(results, key=lambda x: x["id"]):
        ds = r.get("dimension_scores", {})
        status = r.get("status") or "?"
        pm = f"{status:<7}"
        mean_val = r.get("weighted_mean")
        mean_str = f"mean={mean_val:.2f}" if mean_val is not None else "mean=-"
        cost = r.get("cost_usd") or 0.0
        dur = r.get("duration_s")
        err_note = (
            f"  ERR: {r.get('error_reason', '')[:80]}" if status == "ERROR" else ""
        )
        # Short dimension summary: df=4 oo=5 di=4 va=5
        _SHORT = {
            "domain_fit": "df",
            "outcome_orientation": "oo",
            "differentiation": "di",
            "validity": "va",
        }
        dim_parts = []
        for d in dimensions:
            did = d["id"]
            abbr = _SHORT.get(did, did[:2])
            entry = ds.get(did)
            score = entry.get("score", "-") if isinstance(entry, dict) else "-"
            dim_parts.append(f"{abbr}={score}")
        dim_summary = " ".join(dim_parts)
        print(
            f"  {r['id']:3s} {pm}  {dim_summary}  {mean_str}"
            f"  ${cost:.4f}  {f'{dur:.0f}s' if dur else '-':>5}{err_note}"
        )

    if n_error > 0:
        print(
            f"\n⚠ INCOMPLETE — {n_error} infra error(s) after run."
            " Re-run for a valid verdict.",
            file=sys.stderr,
        )
        return 2  # distinct exit code: incomplete

    return 0 if n_failed == 0 else 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    import argparse  # noqa: PLC0415

    ap = argparse.ArgumentParser(
        description=(
            "Phase C eval harness: run wiki-weaver init over schema-design quality scenarios."
        )
    )
    ap.add_argument(
        "--scenarios",
        type=Path,
        default=SCENARIOS_FILE,
        metavar="SCENARIOS_YAML",
        help="Path to the scenarios YAML (default: eval/init_scenarios.yaml).",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="run only the first N scenarios (smoke test)",
    )
    ap.add_argument(
        "--judge-model",
        default=DEFAULT_JUDGE_MODEL,
        metavar="MODEL",
        help=f"LLM model for judge (default: {DEFAULT_JUDGE_MODEL})",
    )
    ap.add_argument(
        "--concurrency",
        type=int,
        default=4,
        metavar="N",
        help="max parallel init runs (default: 4)",
    )
    ap.add_argument(
        "--list",
        action="store_true",
        help="list scenario IDs from the scenarios file and exit without running",
    )
    args = ap.parse_args()

    if args.list:
        scenarios, meta = load_scenarios_and_meta(args.scenarios)
        print(f"Scenarios in {args.scenarios} ({len(scenarios)} total):")
        for sc in scenarios:
            ho = " [held-out]" if sc.get("held_out") else ""
            print(f"  {sc['id']}: {sc.get('domain', '')}{ho}")
        print()
        print(
            "Grader dimensions: "
            + ", ".join(
                f"{d['id']}(w={d['weight']})"
                for d in meta.get("grader", {}).get("dimensions", [])
            )
        )
        return

    sys.exit(
        asyncio.run(
            _run_eval(
                args.scenarios,
                args.limit,
                args.judge_model,
                args.concurrency,
            )
        )
    )


if __name__ == "__main__":
    main()
