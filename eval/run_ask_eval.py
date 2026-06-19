#!/usr/bin/env python3
# pyright: reportMissingImports=false
"""Phase B eval harness — wiki-weaver ask answer-quality scenarios.

For each scenario in eval/ask_scenarios.yaml (9 total: 3 single-page, 3 cross-source,
3 absent-must-refuse):
  1. Runs wiki-weaver ask "<question>" --wiki <wiki> --json via subprocess.
  2. Reads CI metrics from local events.jsonl (cost, wall_time, pages, tools).
  3. Grades the answer with an evidence-based LLM judge: the actual wiki pages the
     answer was built from are injected into the judge prompt so grounding is assessed
     against the real source text — not the judge's priors.
  4. Writes results.json + summary.md to
     ~/.amplifier/evaluation/wiki-weaver/<sortable-datetime>/.

Result status field: PASS | FAIL | ERROR | ?
  ERROR = infrastructure failure (ask subprocess crashed/timed out after retries).
  Only PASS/FAIL count for the suite verdict.
  Suite is INCOMPLETE (exit 2) if any ERROR remains after retries.

Usage:
    python eval/run_ask_eval.py                              # all 9 scenarios
    python eval/run_ask_eval.py --limit 1                   # smoke: one scenario
    python eval/run_ask_eval.py --wiki <dir>                # different wiki
    python eval/run_ask_eval.py --scenarios <file.yaml>     # pinned scenarios file
    python eval/run_ask_eval.py --judge-model <m>           # different judge
    python eval/run_ask_eval.py --concurrency 2             # reduce parallelism
    python eval/run_ask_eval.py --regrade <results.json>    # re-grade saved run, no re-run

Judge pattern: mirrors grade_wiki._build_judge_fn exactly — unified_llm.generate()
wrapped in asyncio.run() for sync compatibility.  In this async harness the judge runs
in a thread via run_in_executor so asyncio.run() can safely create its own event loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml  # type: ignore[import-unresolved]

# ---------------------------------------------------------------------------
# sys.path: let eval/event_metrics import cleanly regardless of cwd
# ---------------------------------------------------------------------------
_EVAL_DIR = Path(__file__).resolve().parent
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from event_metrics import ask_run_metrics  # noqa: E402

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = _EVAL_DIR.parent
SCENARIOS_FILE = _EVAL_DIR / "ask_scenarios.yaml"
DEFAULT_WIKI = REPO_ROOT / "runs" / "corpus" / "wiki"
DEFAULT_JUDGE_MODEL = "claude-sonnet-4-6"
OUTPUT_ROOT = Path.home() / ".amplifier" / "evaluation" / "wiki-weaver"

# ---------------------------------------------------------------------------
# Evidence-injection constants
#
# The grounding judge receives the actual wiki pages the answer was built from,
# so it can verify claims against real text instead of guessing from memory.
# ---------------------------------------------------------------------------

# Max chars to inject per individual wiki page (avoids one huge page swamping others)
_PAGE_CHAR_CAP = 20_000

# Max chars for navigational pages (index.md, overview.md) — large but low-signal
_NAV_PAGE_CHAR_CAP = 3_000

# Hard ceiling on total injected source text across all pages
_TOTAL_CHAR_CAP = 60_000

# Pages treated as navigational (deprioritized vs. content pages)
_NAV_PAGES = frozenset({"index.md", "overview.md"})

# Pages to skip entirely (metadata, not prose evidence)
_SKIP_PAGES = frozenset({".sources.json"})


# ---------------------------------------------------------------------------
# Load scenarios
# ---------------------------------------------------------------------------


def load_scenarios(path: Path = SCENARIOS_FILE) -> list[dict]:
    """Load scenarios from a scenarios YAML file.

    Fails loudly if the file does not exist — no silent fallback.
    """
    if not path.exists():
        sys.exit(f"ERROR: scenarios file not found: {path}")
    raw = path.read_text(encoding="utf-8")
    doc = yaml.safe_load(raw)
    return doc.get("scenarios", [])


# ---------------------------------------------------------------------------
# Error detection helpers
# ---------------------------------------------------------------------------

_ERROR_SIGNATURES = (
    "ask error:",
    "CheckpointMismatchError",
    "Execution failed",
    "Traceback",
    "RuntimeError:",
)


def _is_error_text(text: str) -> bool:
    """Return True if text is empty, an ANSI error banner, or contains error signatures.

    Use for answer validation (empty answer = infrastructure failure).
    For stderr, guard with `if proc.stderr and _is_error_text(proc.stderr)` so that
    an empty (normal) stderr is never treated as an error.
    """
    if not text:
        return True
    if text.startswith("\x1b["):  # ANSI escape — CLI banner, not an answer
        return True
    return any(sig in text for sig in _ERROR_SIGNATURES)


# ---------------------------------------------------------------------------
# Evidence loading — inject actual wiki pages into the judge prompt
# ---------------------------------------------------------------------------


def _load_pages_content(pages_used: list[str], wiki_dir: Path) -> str:
    """Load and concatenate wiki page content for evidence-based grounding.

    Strategy:
    - Skip .sources.json (JSON metadata, not prose evidence).
    - Content pages (non-nav) are processed first in pages_used order, each
      capped at _PAGE_CHAR_CAP chars.
    - Nav pages (index.md, overview.md) are appended last if budget remains,
      capped at _NAV_PAGE_CHAR_CAP chars.
    - If pages_used is empty, fall back to index.md + overview.md (absent scenarios).
    - Hard ceiling: _TOTAL_CHAR_CAP chars total.

    Returns a formatted string ready to inject into the judge prompt.
    Noting truncation so the judge knows the page may continue beyond what it sees.
    """
    # Fallback for empty pages_used (e.g. a refusal scenario)
    effective_pages = pages_used if pages_used else ["index.md", "overview.md"]

    # Skip .sources.json; partition into content vs. nav
    content_pages = [
        p for p in effective_pages if p not in _SKIP_PAGES and p not in _NAV_PAGES
    ]
    nav_pages = [p for p in effective_pages if p in _NAV_PAGES]

    sections: list[str] = []
    total_chars = 0

    def _add_page(page_name: str, char_cap: int) -> None:
        nonlocal total_chars
        if total_chars >= _TOTAL_CHAR_CAP:
            return
        page_path = wiki_dir / page_name
        if not page_path.exists():
            sections.append(f"--- PAGE: {page_name} (NOT FOUND in wiki) ---")
            return
        try:
            text = page_path.read_text(encoding="utf-8")
        except OSError as exc:
            sections.append(f"--- PAGE: {page_name} (READ ERROR: {exc}) ---")
            return

        budget_remaining = _TOTAL_CHAR_CAP - total_chars
        limit = min(char_cap, budget_remaining)
        truncated = len(text) > limit
        chunk = text[:limit]

        header = f"--- PAGE: {page_name} ({len(text)} chars total) ---"
        footer = (
            f"--- [TRUNCATED: showing first {limit} of {len(text)} chars] ---"
            if truncated
            else ""
        )
        section = f"{header}\n{chunk}\n{footer}" if footer else f"{header}\n{chunk}"
        sections.append(section)
        total_chars += len(chunk)

    for p in content_pages:
        _add_page(p, _PAGE_CHAR_CAP)

    for p in nav_pages:
        _add_page(p, _NAV_PAGE_CHAR_CAP)

    if not sections:
        return "(no wiki pages could be loaded)"

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Judge prompt builder — evidence-based, no .format() (avoids { } escaping)
# ---------------------------------------------------------------------------


def _build_judge_prompt(
    sc_type: str,
    question: str,
    answer: str,
    ground_truth: dict,
    source_pages_text: str,
) -> str:
    """Build the full judge prompt with injected source pages.

    Uses f-strings (not .format()) so that { } characters inside source_pages_text,
    question, answer, or ground_truth JSON are treated as literal characters and do
    not interfere with string interpolation.
    """
    ground_truth_json = json.dumps(ground_truth, indent=2)

    if sc_type in ("single_page", "cross_source"):
        return_format = (
            '{"grounded": <1-5>, "cited": <1-5>, "correct": <1-5>,'
            ' "rationale": "<1-2 sentences>"}'
        )
        scoring_extra = """\
grounded  (1-5): Every factual claim traces to the WIKI PAGES PROVIDED ABOVE?
  5 = fully grounded — all claims found in the source pages above.
  1 = contains outside/hallucinated facts NOT present in those pages.
  HARD RULE: one fabricated substantive fact (absent from the pages above) → cap grounded at 1.
  CRITICAL: Do NOT mark a claim as fabricated unless you have confirmed it is ABSENT from the
  provided pages. Scan the text above before judging any specific name/number/attribution.
  If you find a term in the pages (even in a truncated section), it is NOT fabricated.
cited     (1-5): Does the answer name the page(s)/source(s) it drew from?
  5 = correct pages + source tags named. 3 = page named vaguely. 1 = no citation.
correct   (1-5): Does the answer contain the ground_truth key_facts correctly?
  5 = all key facts present & accurate. 3 = partial/imprecise. 1 = wrong or missing."""
    else:  # absent
        return_format = (
            '{"fail_loud": <1-5>, "grounded": <1-5>, "rationale": "<1-2 sentences>"}'
        )
        scoring_extra = """\
fail_loud (1-5): Does the answer explicitly say the wiki doesn't cover the topic WITHOUT
  fabricating content?
  5 = clean refusal, zero invented content. 1 = fabricates an answer or citation.
  IMPORTANT: For absent topics, an answer that names index.md / overview.md / .sources.json
  as "pages consulted to determine absence" is CORRECT and EXPECTED behavior — do NOT penalize
  it for listing pages it checked and found nothing relevant on. The answer checked those pages
  to confirm the topic is absent; that is exactly what it should do.
grounded  (1-5): Did the answer avoid asserting any fabricated facts about the absent topic?
  5 = only states the wiki doesn't cover it (no invented content).
  1 = asserts specific claims about the absent topic as if sourced from the wiki.
  IMPORTANT: Listing navigational pages (index.md, overview.md) as "pages I checked" is
  GROUNDED behavior — those pages exist (you can see them above if included), and the answer
  correctly reported finding nothing relevant on the absent topic."""

    return f"""You are grading a wiki-answering system. Score the answer on each criterion below.
Return ONLY valid JSON — no explanation outside the JSON block.

Return format:
{return_format}

=== ACTUAL WIKI PAGES THE ANSWER WAS BUILT FROM ===
These are the real source pages the answering system read. Use them as your PRIMARY EVIDENCE
when judging GROUNDED. A claim (including specific names, figures, attributions, dates) is
GROUNDED if it appears in or follows from THIS provided text. Do NOT label a specific as
fabricated unless it is ABSENT from the pages below. Quote the supporting snippet when you can.

{source_pages_text}
=== END WIKI PAGES ===

Scoring:
{scoring_extra}

QUESTION:
{question}

ANSWER UNDER TEST:
{answer}

GROUND TRUTH:
{ground_truth_json}
"""


# ---------------------------------------------------------------------------
# wiki-weaver ask subprocess
# ---------------------------------------------------------------------------


def _run_ask_subprocess(
    question: str, wiki: Path
) -> tuple[dict, Path | None, str | None]:
    """Run wiki-weaver ask once and return (result_dict, logs_dir, error_reason).

    error_reason is None on success; a descriptive string on infrastructure failure.
    NEVER uses error banners or stderr as the answer — conflating errors with answers
    was the measurement-integrity bug this function fixes.
    """
    runs_dir = wiki / ".runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    # Snapshot wall time before so we can find the directory this run creates.
    wall_start = time.time()

    # Use the current Python interpreter (which has amplifier_foundation) with
    # PYTHONPATH set to REPO_ROOT so the cli package is importable.
    # This avoids the .venv/bin/wiki-weaver path which lacks amplifier_foundation.
    cmd = [sys.executable, -m", "wiki_weaver", "ask", question, "--wiki", str(wiki), "--json"]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(REPO_ROOT),
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {"answer": "", "pages_used": [], "refused": False}, None, "TIMEOUT"
    except Exception as exc:  # noqa: BLE001
        return (
            {"answer": "", "pages_used": [], "refused": False},
            None,
            f"subprocess error: {exc}",
        )

    # Detect infrastructure failure before parsing — never grade an error banner.
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "")[:300]
        return (
            {"answer": "", "pages_used": [], "refused": False},
            None,
            f"exit {proc.returncode}: {detail}",
        )
    if proc.stderr and _is_error_text(proc.stderr):
        return (
            {"answer": "", "pages_used": [], "refused": False},
            None,
            f"stderr error: {proc.stderr[:300]}",
        )

    stdout = proc.stdout.strip()

    # Extract JSON: find the last { starting at a line boundary (handles leading
    # warning lines like "! asking wiki at ...").
    result: dict = {"answer": "", "pages_used": [], "refused": False}
    for m in reversed(list(re.finditer(r"^\{", stdout, re.MULTILINE))):
        try:
            result = json.loads(stdout[m.start() :])
            break
        except json.JSONDecodeError:
            continue

    # If the parsed answer is an error banner, treat as infrastructure failure.
    answer_text = result.get("answer", "")
    if _is_error_text(answer_text):
        return (
            {"answer": "", "pages_used": [], "refused": False},
            None,
            f"answer is error banner: {answer_text[:200]}",
        )

    # Find the ask- directory created during this run (by mtime).
    logs_dir: Path | None = None
    try:
        ask_dirs = [
            d
            for d in runs_dir.iterdir()
            if d.is_dir()
            and d.name.startswith("ask-")
            and d.stat().st_mtime >= (wall_start - 2)  # 2-second grace window
        ]
        if ask_dirs:
            logs_dir = max(ask_dirs, key=lambda d: d.stat().st_mtime)
    except OSError:
        pass

    return result, logs_dir, None


def _run_ask_with_retry(
    question: str, wiki: Path, max_attempts: int = 3
) -> tuple[dict, Path | None, str | None]:
    """Run ask up to max_attempts times, retrying on infrastructure errors.

    Returns (result_dict, logs_dir, error_reason) — error_reason is None on success.
    With unique run dirs (Fix 1 in engine_runner.py), parallel retries are safe.
    """
    last_reason: str | None = None
    for attempt in range(1, max_attempts + 1):
        result, logs_dir, error_reason = _run_ask_subprocess(question, wiki)
        if error_reason is None:
            return result, logs_dir, None
        last_reason = error_reason
        if attempt < max_attempts:
            print(
                f"    [retry {attempt}/{max_attempts - 1}] infra error: {error_reason[:100]}",
                file=sys.stderr,
            )
    return {"answer": "", "pages_used": [], "refused": False}, None, last_reason


# ---------------------------------------------------------------------------
# LLM judge — mirrors grade_wiki._build_judge_fn exactly
# ---------------------------------------------------------------------------


def _build_judge_fn(model: str = DEFAULT_JUDGE_MODEL):
    """Mirror of grade_wiki._build_judge_fn.

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
# Answer grader — evidence-based
# ---------------------------------------------------------------------------


def _score_scenario(
    scenario: dict,
    answer: str,
    refused: bool,  # noqa: ARG001 — reserved for future absent-detection heuristic
    judge_fn,
    pages_used: list[str],
    wiki_dir: Path,
) -> dict:
    """Grade one scenario with evidence-based grounding.

    Loads the actual wiki pages from pages_used and injects them into the judge
    prompt so grounded is assessed against real source text, not the judge's priors.

    Returns scores dict with per-criterion scores + pass + rationale.
    """
    if judge_fn is None:
        return {
            "grounded": None,
            "cited": None,
            "correct": None,
            "fail_loud": None,
            "rationale": "judge unavailable",
            "pass": None,
        }

    sc_type = scenario.get("type", "")
    question = scenario.get("question", "")
    ground_truth = scenario.get("ground_truth", {})

    # Load source pages for evidence injection
    source_pages_text = _load_pages_content(pages_used, wiki_dir)

    # Build evidence-based prompt (f-strings: safe against { } in source content)
    prompt = _build_judge_prompt(
        sc_type, question, answer, ground_truth, source_pages_text
    )

    try:
        raw = judge_fn(prompt)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        scores: dict = (
            json.loads(m.group(0)) if m else {"rationale": f"parse error: {raw[:200]}"}
        )
    except Exception as exc:  # noqa: BLE001
        scores = {"rationale": f"judge error: {exc}"}

    # Gate: PASS/FAIL per scenario type
    passed: bool | None = None
    if sc_type in ("single_page", "cross_source"):
        g = scores.get("grounded")
        c = scores.get("correct")
        ci = scores.get("cited")
        if g is not None and c is not None and ci is not None:
            passed = False if g == 1 else (g >= 4 and c >= 4 and ci >= 3)
    elif sc_type == "absent":
        fl = scores.get("fail_loud")
        g = scores.get("grounded")
        if fl is not None and g is not None:
            passed = fl >= 4 and g >= 4

    scores["pass"] = passed
    return scores


# ---------------------------------------------------------------------------
# Single scenario runner
# ---------------------------------------------------------------------------


async def _run_one(
    scenario: dict,
    wiki: Path,
    judge_fn,
    sem: asyncio.Semaphore,
) -> dict:
    """Run ask → collect metrics → grade. Returns full result dict.

    status field: "PASS" | "FAIL" | "ERROR" | "?"
    ERROR means an infrastructure failure — the ask subprocess crashed after retries.
    Only PASS/FAIL results are valid quality measurements.
    """
    async with sem:
        loop = asyncio.get_event_loop()

        # 1. Ask (blocking subprocess → thread pool; retries on infra error)
        ask_result, logs_dir, error_reason = await loop.run_in_executor(
            None, _run_ask_with_retry, scenario["question"], wiki
        )

        # Infrastructure error — skip CI metrics and judge; do not grade.
        if error_reason is not None:
            return {
                "id": scenario["id"],
                "type": scenario["type"],
                "held_out": scenario.get("held_out", False),
                "question": scenario["question"],
                "answer": "",
                "pages_used": [],
                "refused": False,
                "logs_dir": None,
                "scores": {},
                "pass": None,
                "status": "ERROR",
                "error_reason": error_reason,
                "cost_usd": 0.0,
                "wall_time_s": None,
                "pages_read": 0,
                "tool_calls": 0,
                "events_found": False,
            }

        # 2. CI metrics (deterministic, fast — OK in executor too)
        metrics = await loop.run_in_executor(
            None, ask_run_metrics, wiki, logs_dir if logs_dir else Path("/dev/null")
        )

        pages_used: list[str] = ask_result.get("pages_used", [])

        # 3. Grade (judge_fn calls asyncio.run() — must run in thread so it
        #    gets its own event loop; safe from run_in_executor threads).
        #    Pass pages_used + wiki so judge sees the actual source text.
        scores = await loop.run_in_executor(
            None,
            _score_scenario,
            scenario,
            ask_result.get("answer", ""),
            ask_result.get("refused", False),
            judge_fn,
            pages_used,
            wiki,
        )

    passed = scores.get("pass")
    status = "PASS" if passed is True else ("FAIL" if passed is False else "?")
    return {
        "id": scenario["id"],
        "type": scenario["type"],
        "held_out": scenario.get("held_out", False),
        "question": scenario["question"],
        "answer": ask_result.get("answer", ""),
        "pages_used": pages_used,
        "refused": bool(ask_result.get("refused", False)),
        "logs_dir": str(logs_dir) if logs_dir else None,
        "scores": scores,
        "pass": passed,
        "status": status,
        "error_reason": None,
        "cost_usd": metrics.get("cost_usd", 0.0),
        "wall_time_s": metrics.get("wall_time_s"),
        "pages_read": metrics.get("pages_read", 0),
        "tool_calls": metrics.get("tool_calls", 0),
        "events_found": metrics.get("events_found", False),
    }


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------


def _write_summary(
    results: list[dict], out_dir: Path, regrade_source: str | None = None
) -> None:
    """Write summary.md to out_dir."""
    total = len(results)
    n_passed = sum(1 for r in results if r.get("pass") is True)
    n_failed = sum(1 for r in results if r.get("pass") is False)
    n_error = sum(1 for r in results if r.get("status") == "ERROR")
    quality_total = total - n_error  # only non-ERROR results count for the verdict

    held_out = [r for r in results if r.get("held_out")]
    held_passed = sum(1 for r in held_out if r.get("pass") is True)

    total_cost = sum(r.get("cost_usd") or 0.0 for r in results)
    wall_times = [r["wall_time_s"] for r in results if r.get("wall_time_s") is not None]
    avg_wall = sum(wall_times) / len(wall_times) if wall_times else None

    if n_error > 0:
        suite_verdict = "INCOMPLETE"
    elif n_failed == 0 and quality_total > 0:
        suite_verdict = "PASS"
    else:
        suite_verdict = "FAIL"

    lines: list[str] = [
        "# wiki-weaver ask — Phase B Eval Summary",
        "",
    ]
    if regrade_source:
        lines += [
            f"_(Re-graded from: `{regrade_source}`)_",
            "",
        ]
    lines += [
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
        f"- Avg wall time: **{avg_wall:.1f}s**" if avg_wall else "- Avg wall time: n/a",
        (
            f"- Per-scenario avg cost: **${total_cost / quality_total:.4f}**"
            if quality_total
            else ""
        ),
        "",
        "## Per-Scenario Results",
        "",
        "| ID | Type | H/O | STATUS | g | ci | k | fl | cost_usd | wall_s | pages |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(results, key=lambda x: x["id"]):
        sc = r.get("scores", {})
        ho = "✓" if r.get("held_out") else ""
        pm = r.get("status") or (
            "PASS"
            if r.get("pass") is True
            else ("FAIL" if r.get("pass") is False else "?")
        )
        cost = r.get("cost_usd") or 0.0
        wt = r.get("wall_time_s")
        err_note = (
            f" _{r.get('error_reason', '')[:60]}_" if r.get("status") == "ERROR" else ""
        )
        lines.append(
            f"| {r['id']} | {r['type']} | {ho} | **{pm}**{err_note} "
            f"| {sc.get('grounded', '-')} | {sc.get('cited', '-')} "
            f"| {sc.get('correct', '-')} | {sc.get('fail_loud', '-')} "
            f"| ${cost:.4f} | {f'{wt:.1f}' if wt else '-'} | {r.get('pages_read', 0)} |"
        )

    lines += [
        "",
        "## Held-out Scenario Detail",
        "",
        "_(Held-out IDs are the real generalization signal — not tuned against.)_",
        "",
    ]
    for r in [x for x in results if x.get("held_out")]:
        sc = r.get("scores", {})
        verdict = r.get("status") or ("PASS" if r.get("pass") is True else "FAIL")
        lines += [
            f"### {r['id']} ({r['type']}) — {verdict}",
            f"**Q:** {r['question']}",
            "",
            f"**Answer (excerpt):** {str(r.get('answer', ''))[:400]}",
            "",
            f"**Scores:** grounded={sc.get('grounded', '-')} "
            f"cited={sc.get('cited', '-')} "
            f"correct={sc.get('correct', '-')} "
            f"fail_loud={sc.get('fail_loud', '-')}",
            "",
            f"**Rationale:** {sc.get('rationale', 'n/a')}",
            "",
        ]

    lines += [
        "## Notes",
        "",
        "- `g`=grounded, `ci`=cited, `k`=correct, `fl`=fail_loud (absent only)",
        "- `H/O` = held-out (generalization guard; do not tune prompts against these)",
        "- Gate: answerable PASS = g≥4 AND k≥4 AND ci≥3; absent PASS = fl≥4 AND g≥4",
        "- Hard fail: any fabricated fact → g=1 → FAIL regardless of other scores",
        "- ERROR = infrastructure failure (ask crashed/timed out after retries); excluded from verdict",
        "- Grounding is evidence-based: judge receives the actual wiki pages the answer was built from.",
    ]

    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# --regrade mode: re-judge saved results without re-running asks
# ---------------------------------------------------------------------------


async def _run_regrade(
    results_json: Path,
    wiki: Path,
    scenarios_path: Path,
    judge_model: str,
) -> int:
    """Re-grade a saved results.json without re-running asks.

    Loads the saved answers + pages_used + metrics from results_json, then runs
    ONLY the evidence-based judge over each (no wiki-weaver ask subprocess calls).
    Writes a fresh results.json + summary.md to a new timestamped output dir.
    """
    print(f"Re-grading: {results_json}")
    print(f"Wiki dir:   {wiki}")
    print(f"Judge:      {judge_model}")
    print()

    saved: list[dict] = json.loads(results_json.read_text(encoding="utf-8"))

    # Build scenario index for ground_truth / type lookup
    scenarios_by_id = {sc["id"]: sc for sc in load_scenarios(scenarios_path)}

    judge_fn = _build_judge_fn(judge_model)
    if judge_fn is None:
        print("ERROR: judge unavailable — cannot regrade.", file=sys.stderr)
        return 1

    loop = asyncio.get_event_loop()
    results: list[dict] = []

    for saved_r in saved:
        sc_id = saved_r.get("id", "?")
        print(f"  re-judging {sc_id} ...", end=" ", flush=True)

        # ERROR results: preserve as-is (no answer to re-judge)
        if saved_r.get("status") == "ERROR":
            print("ERROR (preserved)")
            results.append(dict(saved_r))
            continue

        scenario = scenarios_by_id.get(sc_id)
        if scenario is None:
            print(f"SKIP (scenario {sc_id!r} not found in scenarios file)")
            results.append(dict(saved_r))
            continue

        answer = saved_r.get("answer", "")
        refused = saved_r.get("refused", False)
        pages_used = saved_r.get("pages_used", [])

        # Run judge in executor (it calls asyncio.run() inside — needs own thread loop)
        scores = await loop.run_in_executor(
            None,
            _score_scenario,
            scenario,
            answer,
            refused,
            judge_fn,
            pages_used,
            wiki,
        )

        passed = scores.get("pass")
        status = "PASS" if passed is True else ("FAIL" if passed is False else "?")
        print(
            f"{status}  g={scores.get('grounded', '-')} ci={scores.get('cited', '-')} k={scores.get('correct', '-')} fl={scores.get('fail_loud', '-')}"
        )

        new_r = dict(saved_r)
        new_r["scores"] = scores
        new_r["pass"] = passed
        new_r["status"] = status
        results.append(new_r)

    # Write fresh output dir
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = OUTPUT_ROOT / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "results.json").write_text(
        json.dumps(results, indent=2, default=str), encoding="utf-8"
    )
    _write_summary(results, out_dir, regrade_source=str(results_json))

    # Console summary
    n_passed = sum(1 for r in results if r.get("pass") is True)
    n_failed = sum(1 for r in results if r.get("pass") is False)
    n_error = sum(1 for r in results if r.get("status") == "ERROR")
    quality_total = len(results) - n_error
    held = [r for r in results if r.get("held_out")]
    held_passed = sum(1 for r in held if r.get("pass") is True)
    total_cost = sum(r.get("cost_usd") or 0.0 for r in results)

    print(f"\nRegrade results → {out_dir}")
    print(
        f"Suite: {n_passed}/{quality_total} passed"
        + (f", {n_failed} failed" if n_failed else "")
        + (f", {n_error} ERROR (infra)" if n_error else "")
        + f"  |  Held-out: {held_passed}/{len(held)}"
        + f"  |  Total cost (from saved): ${total_cost:.4f}"
    )
    print()
    for r in sorted(results, key=lambda x: x["id"]):
        sc = r.get("scores", {})
        status = r.get("status") or "?"
        pm = f"{status:<7}"
        cost = r.get("cost_usd") or 0.0
        wt = r.get("wall_time_s")
        err_note = (
            f"  ERR: {r.get('error_reason', '')[:60]}" if status == "ERROR" else ""
        )
        print(
            f"  {r['id']:3s} {pm}  "
            f"g={sc.get('grounded', '-')} ci={sc.get('cited', '-')} "
            f"k={sc.get('correct', '-')} fl={sc.get('fail_loud', '-')}  "
            f"${cost:.4f}  {f'{wt:.1f}s' if wt else '-':>6}  "
            f"{r.get('pages_read', 0)}pg{err_note}"
        )

    if n_error > 0:
        print(
            f"\n⚠ INCOMPLETE — {n_error} infra error(s) after retries."
            " Re-run for a valid verdict.",
            file=sys.stderr,
        )
        return 2

    return 0 if n_failed == 0 else 1


# ---------------------------------------------------------------------------
# Main eval runner
# ---------------------------------------------------------------------------


async def _run_eval(
    wiki: Path,
    scenarios_path: Path,
    limit: int | None,
    judge_model: str,
    concurrency: int = 4,
) -> int:
    scenarios = load_scenarios(scenarios_path)
    if limit is not None:
        scenarios = scenarios[:limit]

    judge_fn = _build_judge_fn(judge_model)
    sem = asyncio.Semaphore(concurrency)

    print(f"Running {len(scenarios)} scenario(s) against wiki: {wiki}")
    print(f"Judge model: {judge_model}  |  Concurrency: {concurrency}")
    if judge_fn is None:
        print("WARN: judge unavailable — scores will be None, all gates will be ?")
    print()

    tasks = [_run_one(sc, wiki, judge_fn, sem) for sc in scenarios]
    results: list[dict] = list(await asyncio.gather(*tasks))

    # Write output
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = OUTPUT_ROOT / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "results.json").write_text(
        json.dumps(results, indent=2, default=str), encoding="utf-8"
    )
    _write_summary(results, out_dir)

    # Console summary
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
        sc = r.get("scores", {})
        status = r.get("status") or "?"
        pm = f"{status:<7}"
        ev_mark = "" if r.get("events_found") else " [no CI events]"
        cost = r.get("cost_usd") or 0.0
        wt = r.get("wall_time_s")
        err_note = (
            f"  ERR: {r.get('error_reason', '')[:60]}" if status == "ERROR" else ""
        )
        print(
            f"  {r['id']:3s} {pm}  "
            f"g={sc.get('grounded', '-')} ci={sc.get('cited', '-')} "
            f"k={sc.get('correct', '-')} fl={sc.get('fail_loud', '-')}  "
            f"${cost:.4f}  {f'{wt:.1f}s' if wt else '-':>6}  "
            f"{r.get('pages_read', 0)}pg{ev_mark}{err_note}"
        )

    if n_error > 0:
        print(
            f"\n⚠ INCOMPLETE — {n_error} infra error(s) after retries."
            " Re-run for a valid verdict.",
            file=sys.stderr,
        )
        return 2  # distinct exit code: incomplete (not a quality failure)

    return 0 if n_failed == 0 else 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    import argparse  # noqa: PLC0415

    ap = argparse.ArgumentParser(
        description="Phase B eval harness: run wiki-weaver ask over answer-quality scenarios."
    )
    ap.add_argument(
        "--wiki",
        type=Path,
        default=DEFAULT_WIKI,
        help=f"wiki directory (default: {DEFAULT_WIKI})",
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
        help="max parallel ask subprocesses (default: 4)",
    )
    ap.add_argument(
        "--scenarios",
        type=Path,
        default=SCENARIOS_FILE,
        metavar="SCENARIOS_YAML",
        help="Path to the scenarios YAML (default: eval/ask_scenarios.yaml).",
    )
    ap.add_argument(
        "--regrade",
        type=Path,
        default=None,
        metavar="RESULTS_JSON",
        help=(
            "Re-grade a saved results.json without re-running asks. "
            "Loads saved answers + pages_used, re-runs only the evidence-based judge. "
            "Writes fresh results.json + summary.md to a new timestamped output dir."
        ),
    )
    args = ap.parse_args()

    if args.regrade is not None:
        sys.exit(
            asyncio.run(
                _run_regrade(args.regrade, args.wiki, args.scenarios, args.judge_model)
            )
        )
    else:
        sys.exit(
            asyncio.run(
                _run_eval(
                    args.wiki,
                    args.scenarios,
                    args.limit,
                    args.judge_model,
                    args.concurrency,
                )
            )
        )


if __name__ == "__main__":
    main()
