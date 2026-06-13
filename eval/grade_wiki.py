# pyright: reportMissingImports=false
#!/usr/bin/env python3
"""Lightweight, reusable graders for a woven wiki (PIPELINE_DESIGN.md §6).

Deliberately SMALL: high-signal checks the orchestrator can re-run, built on
top of the existing deterministic validator (pipeline/validate_wiki.py) plus a
ledger-/registry-integrity check. No framework, no new abstractions.

Graders
-------
- structural_clean(wiki)   reuse validate_wiki: 0 broken / 0 orphan / 0 uncited.
- no_duplicate_pages(wiki)  no merge-fragment duplicates (slug-N.md alongside
                            slug.md); version-named entities (gemma-4.md,
                            deepseek-v3-2.md) are NOT flagged.
- ledger_integrity(wiki)    every ledger line is a REAL convergence: converged
                            == true, logs_dir exists, distinct source ids, and
                            the row's source id matches the .sources.json
                            registry (no confabulated lines).
- merge_accrual(wiki, page, expected_ids)
                            a shared concept page's frontmatter ``sources:``
                            accrued exactly the expected ids (compounding).

Scenario checks (encode the two design §6 scenarios as runnable asserts)
- grade_converge(wiki, expected_source_ids)  S-converge: a cluster of
  overlapping sources ALL converged + archived, validator clean, ids distinct,
  no duplicate pages, and at least one page accrued >1 source (real merge).
- grade_recover(before_failures, after_passed)  S-recover: an injected broken
  link was present (before) and is repaired (after validator exit 0).

Synthesis quality grader
------------------------
- grade_synthesis(wiki, judge_fn=None)  deterministic gates (G0, G1) + optional
  LLM corroboration (G3). Ratio metrics are diagnostics only — not gates.
  Runs without any network or LLM when judge_fn=None.
  CLI: ``python grade_wiki.py synthesis <wiki_dir> [--judge]``

CLI
---
    python grade_wiki.py converge <wiki_dir> --sources 1 2 3
    python grade_wiki.py integrity <wiki_dir>
    python grade_wiki.py synthesis <wiki_dir>
    python grade_wiki.py synthesis <wiki_dir> --judge

Exit 0 == all graded checks pass; non-zero == at least one failed (prints why).
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

# Reuse the single deterministic validator (DRY — same artifact the pipeline's
# validate node runs).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
from validate_wiki import validate  # noqa: E402

LEDGER_NAME = ".processed.jsonl"
REGISTRY_NAME = ".sources.json"
ARCHIVE = "_archive"
# Numeric-suffix slug pattern used to detect merge-fragment duplicates.
# See no_duplicate_pages() — the regex alone is not enough; we also require
# the base slug to exist so legitimate version-named pages (gemma-4.md,
# deepseek-v3-2.md, kimi-k2-5.md, …) are NOT flagged as duplicates.
DUP_PAGE = re.compile(r"-\d+\.md$")
# Parse a frontmatter ``sources: [1, 2]`` list into a set of ints.
SOURCES_FM = re.compile(r"^sources:\s*\[([^\]]*)\]", re.MULTILINE)

# ---------------------------------------------------------------------------
# Synthesis-quality constants
# ---------------------------------------------------------------------------

# Inline citation: "[12]" or "[3, 12]"
INLINE_CITE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")

# ATX section header at h2-h4 level
SECTION_HEADER = re.compile(r"(?m)^(#{2,4})\s+(.*)$")

# Headers that name a source — three patterns (A1/A2/A3).  Any match is a
# "source-labeled section" (concatenation signal, not synthesis).
LABELED_HEADER: list[re.Pattern[str]] = [
    # A1 parenthetical: "## Title (from Source N)" / "## X (Source 3, 5)"
    re.compile(
        r"^#{1,6}\s+.*\((?:from\s+|per\s+|via\s+|see\s+|according\s+to\s+)?"
        r"[Ss]ources?\s+\d+(?:\s*[,&]\s*\d+)*\)\s*$",
        re.IGNORECASE,
    ),
    # A2 trailing-colon: "## from Source 12: ..." or "## Source 3 — ..."
    re.compile(
        r"^#{1,6}\s+(?:from\s+)?[Ss]ources?\s+\d+\s*[:\-\u2014].*$",
        re.IGNORECASE,
    ),
    # A3 bracket-id: "## Title (Source [12])" or "## X (Source [3], [5])"
    re.compile(
        r"^#{1,6}\s+.*\(?\bSources?\s*\[\d+\](?:\s*,\s*\[\d+\])*\)?\s*$",
        re.IGNORECASE,
    ),
]

# Sentence openers that narrate by source rather than by topic.
# "Source [N] describes…" / "Source N notes…" etc.
NARRATION_OPENER = re.compile(
    r"(?:^|(?<=\. ))"
    r"Source[s]?\s+(?:\[\d+\]|\d+)(?:\s*[,&]\s*(?:\[\d+\]|\d+))*"
    r"\s+(?:also\s+)?"
    r"(?:describes?|reports?|notes?|states?|argues?|explains?|identifies?"
    r"|presents?|covers?|lists?|suggests?|highlights?|introduces?"
    r"|discusses?|warns?|recommends?|frames?|defines?|cites?|adds?"
    r"|proposes?|shows?|documents?|emphasizes?|concludes?|contends?"
    r"|calls?|distinguishes?|traces?|addresses?|attributes?)\b",
    re.IGNORECASE | re.MULTILINE,
)


class GradeResult:
    """Tiny PASS/FAIL accumulator (one per scenario)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.failures: list[str] = []
        self.notes: list[str] = []

    def check(self, ok: bool, fail_msg: str, ok_msg: str | None = None) -> None:
        if ok:
            if ok_msg:
                self.notes.append(ok_msg)
        else:
            self.failures.append(fail_msg)

    @property
    def passed(self) -> bool:
        return not self.failures

    def report(self) -> str:
        lines = [f"[{'PASS' if self.passed else 'FAIL'}] {self.name}"]
        for n in self.notes:
            lines.append(f"    · {n}")
        for f in self.failures:
            lines.append(f"    ✗ {f}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Primitive graders
# ---------------------------------------------------------------------------


def structural_clean(wiki: Path) -> tuple[bool, list[str]]:
    r = validate(wiki)
    return bool(r.get("passed")), list(r.get("failures", []))


def no_duplicate_pages(wiki: Path) -> list[str]:
    """Return fragment pages whose base slug already exists as a separate page.

    Catches per-source duplicate concept fragments (e.g. concept-2.md when
    concept.md also exists) while ignoring legitimate version- or number-named
    entity pages like gemma-4.md, deepseek-v3-2.md, kimi-k2-5.md (no matching
    base page present in the wiki).
    """
    dups = []
    for p in sorted(wiki.glob("*.md")):
        if not DUP_PAGE.search(p.name):
            continue
        # Only flag when the de-numbered base slug also exists as a page.
        base_name = DUP_PAGE.sub(".md", p.name)
        if (wiki / base_name).is_file():
            dups.append(p.name)
    return dups


def _read_ledger(wiki: Path) -> list[dict]:
    led = wiki / LEDGER_NAME
    if not led.exists():
        return []
    rows: list[dict] = []
    for line in led.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _registry_ids(wiki: Path) -> set[int]:
    reg = wiki / REGISTRY_NAME
    if not reg.exists():
        return set()
    try:
        data = json.loads(reg.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    return {
        int(e["id"]) for e in data.get("sources", []) if isinstance(e.get("id"), int)
    }


def ledger_integrity(wiki: Path) -> GradeResult:
    """Every ledger line is a real convergence with a real logs_dir.

    Confabulation guard already reverts agent-written lines; this asserts the
    surviving lines are well-formed CLI convergences and distinct.
    """
    res = GradeResult("ledger-integrity")
    rows = _read_ledger(wiki)
    res.check(bool(rows), "ledger is empty (no converged sources)")
    reg_ids = _registry_ids(wiki)
    seen_ids: list[int] = []
    for i, row in enumerate(rows):
        tag = f"ledger[{i}] {row.get('source', '?')}"
        # A genuine CLI convergence line carries these fields (an old/confabulated
        # line with source_number/file_path would be flagged here).
        res.check(
            row.get("converged") is True,
            f"{tag}: converged is not True ({row.get('converged')!r})",
        )
        ld = row.get("logs_dir")
        res.check(
            bool(ld) and Path(ld).is_dir(),
            f"{tag}: logs_dir missing or not a real dir ({ld!r})",
        )
        sid = row.get("source_id")
        res.check(sid is not None, f"{tag}: no source_id field")
        if sid is not None:
            seen_ids.append(int(sid))
            res.check(
                (not reg_ids) or int(sid) in reg_ids,
                f"{tag}: source_id {sid} not in registry {sorted(reg_ids)}",
            )
        archived = row.get("archived_to")
        res.check(
            bool(archived) and Path(archived).is_file(),
            f"{tag}: archived_to missing on disk ({archived!r})",
        )
    res.check(
        len(seen_ids) == len(set(seen_ids)),
        f"duplicate source ids in ledger: {seen_ids}",
        f"{len(seen_ids)} distinct converged source id(s): {sorted(set(seen_ids))}",
    )
    return res


def page_sources(wiki: Path, page_name: str) -> set[int]:
    """The set of source ids in a page's frontmatter ``sources:`` list."""
    p = wiki / page_name
    if not p.is_file():
        return set()
    m = SOURCES_FM.search(p.read_text(encoding="utf-8", errors="replace"))
    if not m:
        return set()
    ids: set[int] = set()
    for tok in m.group(1).split(","):
        tok = tok.strip()
        if tok.isdigit():
            ids.add(int(tok))
    return ids


def _sources_from_text(text: str) -> set[int]:
    """Parse frontmatter ``sources: [...]`` from raw page text into a set of ints."""
    m = SOURCES_FM.search(text)
    if not m:
        return set()
    return {int(t.strip()) for t in m.group(1).split(",") if t.strip().isdigit()}


def max_source_accrual(wiki: Path) -> tuple[str, set[int]]:
    """Return the content page that accrued the MOST source ids (merge proof)."""
    best_page = ""
    best: set[int] = set()
    for p in sorted(wiki.glob("*.md")):
        ids = page_sources(wiki, p.name)
        if len(ids) > len(best):
            best, best_page = ids, p.name
    return best_page, best


# ---------------------------------------------------------------------------
# Scenario checks (design §6)
# ---------------------------------------------------------------------------


def grade_converge(wiki: Path, expected_source_ids: list[int]) -> GradeResult:
    """S-converge: a cluster of overlapping sources ALL converge + archive,
    with a clean validator and a correct (compounding) merge.
    """
    res = GradeResult(f"S-converge (sources {expected_source_ids})")

    ok, failures = structural_clean(wiki)
    res.check(ok, f"validator FAIL: {'; '.join(failures)}", "validator clean (exit 0)")

    dups = no_duplicate_pages(wiki)
    res.check(
        not dups, f"duplicate concept pages present: {dups}", "no duplicate pages"
    )

    # All expected sources are archived + on the ledger as converged.
    rows = _read_ledger(wiki)
    converged_ids = {
        int(r["source_id"])
        for r in rows
        if r.get("converged") is True and r.get("source_id") is not None
    }
    missing = sorted(set(expected_source_ids) - converged_ids)
    res.check(
        not missing,
        f"source ids never converged: {missing} (ledger has {sorted(converged_ids)})",
        f"all {len(expected_source_ids)} sources converged: {sorted(converged_ids)}",
    )
    arc = wiki / ARCHIVE
    archived = {p.name for p in arc.iterdir()} if arc.is_dir() else set()
    res.check(
        len(archived) >= len(expected_source_ids),
        f"archive has {len(archived)} file(s), expected >= {len(expected_source_ids)}",
        f"{len(archived)} source(s) archived",
    )

    led = ledger_integrity(wiki)
    res.failures.extend(led.failures)
    res.notes.extend(led.notes)

    # Real merge: at least one page accrued more than one source id.
    page, ids = max_source_accrual(wiki)
    res.check(
        len(ids) >= 2,
        "no page accrued >1 source id — sources did not compound (merge failed)",
        f"merge proof: {page} accrued sources {sorted(ids)}",
    )
    return res


def grade_recover(before_failures: list[str], after_passed: bool) -> GradeResult:
    """S-recover: a deliberately injected broken link was present before, and
    the loop repaired it (validator back to exit 0) within the cycle budget.
    """
    res = GradeResult("S-recover (injected broken link repaired)")
    res.check(
        bool(before_failures),
        "no failures before — nothing to recover from (injection didn't take)",
        f"injected failure observed: {'; '.join(before_failures)}",
    )
    res.check(
        after_passed,
        "validator still FAILS after refine — loop did NOT close",
        "validator clean after refine — loop closed the failure",
    )
    return res


# ---------------------------------------------------------------------------
# Synthesis-quality grader  (deterministic primary; LLM judge secondary)
# ---------------------------------------------------------------------------


def multi_source_pages(wiki: Path) -> list[Path]:
    """Pages with >=2 source ids in frontmatter, excluding overview.md and type:index."""
    _TYPE_FM = re.compile(r"^type:\s*(\S+)", re.MULTILINE)
    pages: list[Path] = []
    for p in sorted(wiki.glob("*.md")):
        if p.name == "overview.md":
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        tm = _TYPE_FM.search(text)
        if tm and tm.group(1).strip() == "index":
            continue
        if len(_sources_from_text(text)) >= 2:
            pages.append(p)
    return pages


def source_labeled_sections(page_text: str) -> list[str]:
    """Return all header lines that name a source (Metric A).

    len() == 0 is the pass condition for a fully synthesized page.
    """
    return [
        line
        for line in page_text.splitlines()
        if any(rx.match(line) for rx in LABELED_HEADER)
    ]


def single_source_section_ratio(
    page_text: str,
) -> tuple[float | None, int, int]:
    """Fraction of cited sections that cite exactly one distinct source (Metric B).

    Returns:
        (ratio, single_source_sections, cited_sections)
        ratio is None when no section cites anything (page has no inline cites).
    """
    # Split on h2-h4 header lines; keep the text between them as sections.
    sections = re.split(r"(?m)^#{2,4}\s+.*$", page_text)
    cited_sections = 0
    single_source_sections = 0
    for section in sections:
        ids: set[int] = set()
        for m in INLINE_CITE.finditer(section):
            for part in m.group(1).split(","):
                part = part.strip()
                if part.isdigit():
                    ids.add(int(part))
        if ids:
            cited_sections += 1
            if len(ids) == 1:
                single_source_sections += 1
    if cited_sections == 0:
        return None, 0, 0
    return (
        single_source_sections / cited_sections,
        single_source_sections,
        cited_sections,
    )


def near_dup_heading_pairs(page_text: str) -> int:
    """Count same-depth heading pairs with Jaccard(titles) >= 0.5 AND disjoint source
    sets in their section bodies (Metric C — secondary).
    """
    _STOPWORDS = frozenset(
        "the a an and or of in to for is are with from by on at as its"
        " it this that these those be been was were".split()
    )

    def _tokens(title: str) -> frozenset[str]:
        toks = re.split(r"[^a-z0-9]+", title.lower())
        return frozenset(t for t in toks if len(t) > 2 and t not in _STOPWORDS)

    def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
        union = a | b
        return len(a & b) / len(union) if union else 1.0

    def _section_source_ids(body: str) -> frozenset[int]:
        ids: set[int] = set()
        for m in INLINE_CITE.finditer(body):
            for part in m.group(1).split(","):
                part = part.strip()
                if part.isdigit():
                    ids.add(int(part))
        return frozenset(ids)

    # Walk lines to build (depth, title_tokens, body_source_ids) per section.
    sections: list[tuple[int, frozenset[str], frozenset[int]]] = []
    cur_depth = 0
    cur_title = ""
    cur_body: list[str] = []

    for line in page_text.splitlines():
        hm = SECTION_HEADER.match(line)
        if hm:
            depth = len(hm.group(1))
            if depth in (2, 3, 4):
                if cur_title:
                    sections.append(
                        (
                            cur_depth,
                            _tokens(cur_title),
                            _section_source_ids("\n".join(cur_body)),
                        )
                    )
                cur_depth = depth
                cur_title = hm.group(2).strip()
                cur_body = []
        else:
            cur_body.append(line)

    if cur_title:
        sections.append(
            (
                cur_depth,
                _tokens(cur_title),
                _section_source_ids("\n".join(cur_body)),
            )
        )

    count = 0
    for i in range(len(sections)):
        for j in range(i + 1, len(sections)):
            di, ti, si = sections[i]
            dj, tj, sj = sections[j]
            if di != dj:
                continue
            if _jaccard(ti, tj) >= 0.5 and si.isdisjoint(sj):
                count += 1
    return count


def source_narration(page_text: str) -> tuple[int, float]:
    """Count NARRATION_OPENER matches and their density (Metric D — diagnostic only).

    Returns:
        (opener_count, opener_count / body_paragraph_count)
    """
    opener_count = len(NARRATION_OPENER.findall(page_text))

    # Strip YAML frontmatter before counting paragraphs.
    body = page_text
    if page_text.startswith("---"):
        idx = page_text.find("\n---", 3)
        if idx != -1:
            body = page_text[idx + 4 :]

    in_fence = False
    paragraph_count = 0
    for block in re.split(r"\n{2,}", body.strip()):
        b = block.strip()
        if not b:
            continue
        if b.startswith("```") or b.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if re.match(r"^#{1,6}\s", b):
            continue
        paragraph_count += 1

    return opener_count, opener_count / max(1, paragraph_count)


def grade_integration(wiki: Path) -> dict:
    """Roll up synthesis metrics A-D over all multi-source pages.

    Returns a dict with:
        multi_page_count, total_source_labeled_sections, pct_multi_pages_labeled,
        median_single_source_ratio, pct_multi_pages_over_0_7,
        pages_with_redundancy, per_page (list), deterministic_pass (bool).
    """
    pages = multi_source_pages(wiki)
    multi_page_count = len(pages)

    total_labeled = 0
    labeled_pages = 0
    ratios: list[float] = []
    redundancy_pages = 0
    per_page: list[dict] = []

    for p in pages:
        text = p.read_text(encoding="utf-8", errors="replace")

        # Metric A
        n_labeled = len(source_labeled_sections(text))
        total_labeled += n_labeled
        if n_labeled > 0:
            labeled_pages += 1

        # Metric B
        ratio, _, _ = single_source_section_ratio(text)
        if ratio is not None:
            ratios.append(ratio)

        # Metric C
        near_dups = near_dup_heading_pairs(text)
        if near_dups > 0:
            redundancy_pages += 1

        # Metric D (diagnostic)
        opener_count, narration_ratio = source_narration(text)

        per_page.append(
            {
                "slug": p.stem,
                "sources": sorted(_sources_from_text(text)),
                "labeled": n_labeled,
                "ratio": ratio,
                "near_dups": near_dups,
                "narration_ratio": narration_ratio,
            }
        )

    pct_labeled = labeled_pages / multi_page_count if multi_page_count > 0 else 0.0
    median_ratio: float | None = statistics.median(ratios) if ratios else None
    pct_over_0_7 = sum(1 for r in ratios if r > 0.7) / len(ratios) if ratios else 0.0

    g1 = total_labeled == 0
    # g2 (ratio/pct metrics) is computed for diagnostics but is NOT a gate.
    # Demoted: ratio metrics cannot distinguish legit single-source sections
    # from artificial silos on unevenly-covered corpora.

    return {
        "multi_page_count": multi_page_count,
        "total_source_labeled_sections": total_labeled,
        "pct_multi_pages_labeled": pct_labeled,
        "median_single_source_ratio": median_ratio,
        "pct_multi_pages_over_0_7": pct_over_0_7,
        "pages_with_redundancy": redundancy_pages,
        "per_page": per_page,
        "deterministic_pass": g1,  # G1 only; ratio metrics are diagnostics
    }


# ---------------------------------------------------------------------------
# LLM-judge helpers  (isolated behind an injected judge_fn; no network
# when judge_fn is None — the deterministic path has zero LLM dependency)
# ---------------------------------------------------------------------------

_INTEGRATION_RUBRIC = """\
You are grading the SYNTHESIS QUALITY of a wiki page produced by merging multiple source articles.

Score HOW WELL the page merges OVERLAPPING content on a 1–5 scale.

CRITICAL INSTRUCTION: Do NOT lower the score for single-source sections if no other source
on this page covers that subtopic. Single-source coverage of unique subtopics is CORRECT
ATTRIBUTION, not a synthesis failure. Score ONLY whether content that COULD be merged IS merged.

  5 = Wherever two or more sources discuss the SAME subtopic, their claims are fused within
      shared sentences or paragraphs. Single-source sections exist ONLY where genuinely one
      source covers that subtopic. Effectively zero artificial silos.
  4 = Most overlapping subtopics are woven together; a few that clearly overlap remain split,
      but the dominant pattern is integration. Only minor artificial silos.
  3 = Some overlapping subtopics are woven but several that clearly overlap remain split
      across source-organised sections.
  2 = Little integration of overlap — most subtopics that multiple sources discuss are still
      split into per-source sections.
  1 = Overlapping content is consistently siloed by source — even when multiple sources
      discuss the same thing, each source gets its own section.

Identify "artificial_silos": section NAMES where a subtopic covered by ≥2 sources on this
page was NOT merged (the overlap was split rather than integrated). Do NOT list sections that
cover a subtopic discussed by only one source on this page.

Return ONLY valid JSON:
{"score": <1-5>, "rationale": "<one sentence>", "artificial_silos": ["<section name>", ...]}

PAGE:
"""

_FRAMING_RUBRIC = """\
Score how well the page frames vendor/marketing claims vs. verifiable facts on 1–5:
  5 = All vendor claims attributed ("X claims…") or hedged; verifiable facts stated plainly.
  4 = Most claims attributed; occasional hype phrase slips through unflagged.
  3 = Mixed — some claims attributed, others reproduced as fact.
  2 = Mostly laundered — marketing superlatives stated as fact, rare attribution.
  1 = Fully laundered — vendor copy reproduced verbatim as unhedged fact.

Return ONLY valid JSON:
{"score": <1-5>, "rationale": "<one sentence>", "examples": ["<example>", ...]}

PAGE:
"""


def select_judge_sample(integration_result: dict, k: int = 8) -> list[str]:
    """Top-k multi-source page slugs ranked by (sources desc, ratio desc,
    narration_ratio desc) — the pages most likely to expose concatenation."""
    pages = integration_result.get("per_page", [])
    ranked = sorted(
        pages,
        key=lambda p: (
            -len(p["sources"]),
            -(p["ratio"] if p["ratio"] is not None else 0.0),
            -p["narration_ratio"],
        ),
    )
    return [p["slug"] for p in ranked[:k]]


def judge_integration(page_text: str, judge_fn) -> dict:
    """LLM judge: integration quality 1–5.

    Returns {score, rationale, artificial_silos}.
    artificial_silos: section names where overlapping content was wrongly split.
    """
    prompt = _INTEGRATION_RUBRIC + page_text[:6000]
    try:
        raw = judge_fn(prompt)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception:
        pass
    return {"score": 3, "rationale": "parse error", "artificial_silos": []}


def judge_claim_framing(page_text: str, judge_fn) -> dict:
    """LLM judge: claim-framing quality 1–5.  Returns {score, rationale, examples}."""
    prompt = _FRAMING_RUBRIC + page_text[:6000]
    try:
        raw = judge_fn(prompt)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception:
        pass
    return {"score": 3, "rationale": "parse error", "examples": []}


def _build_judge_fn():
    """Wire judge_fn to unified_llm.generate() if importable; else return None.

    Uses the top-level generate() convenience function (Spec §4.3) which takes
    a plain-text ``prompt`` kwarg and returns a GenerateResult with a .text
    attribute.  generate() is async; asyncio.run() bridges the sync CLI caller.
    """
    try:
        import asyncio  # noqa: PLC0415

        from unified_llm import generate  # type: ignore  # noqa: PLC0415

        def _judge(prompt: str) -> str:
            result = asyncio.run(generate("claude-sonnet-4-6", prompt=prompt))
            return result.text

        return _judge
    except Exception as exc:
        print(
            f"WARN: unified_llm not importable ({exc}); "
            "falling back to deterministic-only grading.",
            file=sys.stderr,
        )
        return None


# ---------------------------------------------------------------------------
# Top-level synthesis grader
# ---------------------------------------------------------------------------


def grade_synthesis(wiki: Path, judge_fn=None) -> GradeResult:
    """Grade synthesis quality of a woven wiki.

    Hard gates (deterministic, no LLM):
        G0  structural_clean + no_duplicate_pages + ledger_integrity
        G1  total_source_labeled_sections == 0

    Diagnostics only (NOT gates — ratio metrics penalise legit single-source
    sections on uneven-coverage corpora and cannot distinguish real silos):
        [diag]  median_single_source_ratio  (reported, not gated)
        [diag]  pct_multi_pages_over_0_7    (reported, not gated)

    Optional gate (requires judge_fn):
        G3  mean_integration >= 4.0
            Rubric scores "merge WHERE overlap exists"; single-source sections
            that cover unique subtopics are NOT penalised — only artificial
            silos (overlapping content wrongly split) count against the score.
            total_artificial_silos is reported as a corroborating diagnostic.
            It is NOT an independent gate: score 4 inherently implies "minor
            silos" per the rubric, so a separate silo count gate would
            double-penalise the same signal.

    SynthesisScore 0–100 (or /60 deterministic-only):
        det_integration   = 40 * (1 - labeled_rate)
        llm_integration   = 20 * (mean_int - 1) / 4          [0 when disabled]
        redundancy_score  = 20 * (1 - redundancy_pages / multi_page_count)
        llm_framing       = 20 * (mean_frame - 1) / 4        [0 when disabled]

    Pass judge_fn=None for a fully offline, zero-network run.
    When judge is disabled: gate on G0 + G1 only; quality is uncertified
    without the judge.
    """
    res = GradeResult("synthesis-quality")

    # G0: reuse existing structural preconditions
    struct_ok, struct_failures = structural_clean(wiki)
    res.check(
        struct_ok, f"G0 struct FAIL: {'; '.join(struct_failures)}", "G0 struct clean"
    )

    dups = no_duplicate_pages(wiki)
    res.check(not dups, f"G0 duplicate pages: {dups}", "G0 no duplicate pages")

    led = ledger_integrity(wiki)
    if led.passed:
        res.notes.append("G0 ledger clean")
    else:
        res.failures.extend(led.failures)

    g0_ok = struct_ok and not dups and led.passed

    # Integration metrics
    integ = grade_integration(wiki)
    labeled = integ["total_source_labeled_sections"]
    pct_labeled = integ["pct_multi_pages_labeled"]
    median_ratio = integ["median_single_source_ratio"]
    pct_over_0_7 = integ["pct_multi_pages_over_0_7"]
    redundancy = integ["pages_with_redundancy"]
    multi_count = integ["multi_page_count"]

    # G1
    g1 = labeled == 0
    res.check(
        g1,
        f"G1 FAIL: {labeled} source-labeled sections (target 0)",
        f"G1 source-labeled sections: {labeled}",
    )

    # G2 — DIAGNOSTIC ONLY, NOT a gate.
    # Ratio metrics cannot distinguish legitimate single-source sections (correct
    # attribution, unique subtopics) from artificial silos. Reported for
    # diagnostics; the LLM judge (G3) handles true silo detection.
    ratio_str = f"{median_ratio:.3f}" if median_ratio is not None else "None"

    # Score components
    def _clamp(v: float) -> float:
        return max(0.0, min(1.0, v))

    # Ratio factor dropped — ratio metrics are now diagnostic-only.
    # det_integration scores solely on labeled-section rate (G1 signal).
    det_integration = 40.0 * (1 - _clamp(pct_labeled))
    redundancy_score = 20.0 * (1 - redundancy / max(1, multi_count))

    llm_enabled = judge_fn is not None
    llm_integration = 0.0
    llm_framing = 0.0
    sample_slugs: list[str] = []
    total_artificial_silos = 0
    mean_int = 0.0

    if llm_enabled:
        sample_slugs = select_judge_sample(integ)
        int_scores: list[int] = []
        int_results: list[dict] = []
        frame_scores: list[int] = []
        for slug in sample_slugs:
            p = wiki / f"{slug}.md"
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace")
                ir = judge_integration(text, judge_fn)
                int_scores.append(ir.get("score", 3))
                int_results.append(ir)
                frame_scores.append(judge_claim_framing(text, judge_fn).get("score", 3))
        if int_scores:
            mean_int = sum(int_scores) / len(int_scores)
            llm_integration = 20.0 * (mean_int - 1) / 4
            total_artificial_silos = sum(
                len(r.get("artificial_silos", [])) for r in int_results
            )
            # G3: pass when overlap is genuinely merged (mean_int >= 4.0).
            # A page full of legit single-coverage scores ~5 (no artificial silos
            # to penalise) under the reframed rubric, so it passes naturally.
            # total_artificial_silos is reported as a corroborating diagnostic;
            # it is NOT a separate gate: score 4 inherently implies "minor silos"
            # per the rubric, and a separate count gate would double-penalise.
            g3_pass = mean_int >= 4.0
            res.check(
                g3_pass,
                f"G3 FAIL: mean_integration={mean_int:.2f} (need >=4.0)",
                f"G3 PASS: mean_integration={mean_int:.2f}, "
                f"artificial_silos={total_artificial_silos} (diagnostic)",
            )
        if frame_scores:
            mean_frame = sum(frame_scores) / len(frame_scores)
            llm_framing = 20.0 * (mean_frame - 1) / 4

    # Final score
    if not g0_ok:
        raw_score = 0.0
    elif llm_enabled:
        raw_score = det_integration + llm_integration + redundancy_score + llm_framing
    else:
        raw_score = det_integration + redundancy_score

    score = max(0, min(100, int(raw_score)))
    denom = (
        100 if llm_enabled else 60
    )  # det(40) + redundancy(20) = 60 max without judge

    res.notes += [
        f"SynthesisScore: {score}/{denom}{'(det-only; quality uncertified without judge)' if not llm_enabled else ''}",
        f"multi_page_count: {multi_count}",
        f"total_source_labeled_sections: {labeled}",
        f"pct_multi_pages_labeled: {pct_labeled:.1%}",
        f"[diag] median_single_source_ratio: {ratio_str}",
        f"[diag] pct_multi_pages_over_0_7: {pct_over_0_7:.1%}",
        f"pages_with_redundancy: {redundancy}/{multi_count}",
        f"det_integration: {det_integration:.1f}/40",
        f"redundancy_score: {redundancy_score:.1f}/20",
    ]
    if llm_enabled:
        res.notes += [
            f"llm_integration: {llm_integration:.1f}/20  (mean_int={mean_int:.2f}/5)",
            f"total_artificial_silos: {total_artificial_silos}",
            f"llm_framing: {llm_framing:.1f}/20",
            f"judge_sample: {sample_slugs}",
        ]
    else:
        res.notes.append("llm_enabled: false (gate G3 skipped; quality uncertified)")

    return res


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="Grade a woven wiki (design §6).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_c = sub.add_parser("converge", help="S-converge grader")
    p_c.add_argument("wiki_dir", type=Path)
    p_c.add_argument(
        "--sources",
        type=int,
        nargs="+",
        required=True,
        help="expected converged source ids (e.g. --sources 1 2 3)",
    )

    p_i = sub.add_parser("integrity", help="ledger-integrity grader")
    p_i.add_argument("wiki_dir", type=Path)

    p_s = sub.add_parser(
        "synthesis", help="synthesis-quality grader (deterministic-only by default)"
    )
    p_s.add_argument("wiki_dir", type=Path)
    p_s.add_argument(
        "--judge",
        action="store_true",
        help="add LLM corroboration via unified_llm (requires attractor venv)",
    )

    args = ap.parse_args()
    if not args.wiki_dir.is_dir():
        print(f"FAIL: wiki dir not found: {args.wiki_dir}", file=sys.stderr)
        return 2

    if args.cmd == "converge":
        res = grade_converge(args.wiki_dir, args.sources)
    elif args.cmd == "integrity":
        res = ledger_integrity(args.wiki_dir)
    else:  # synthesis
        judge_fn = _build_judge_fn() if getattr(args, "judge", False) else None
        res = grade_synthesis(args.wiki_dir, judge_fn=judge_fn)

    print(res.report())
    return 0 if res.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
