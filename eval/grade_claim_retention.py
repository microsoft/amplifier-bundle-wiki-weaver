# pyright: reportMissingImports=false
#!/usr/bin/env python3
"""Claim-retention grader for wiki-weaver's Phase A re-write path.

Answers: when a new source forces a page re-write, does any previously-grounded
claim SILENTLY DISAPPEAR with no trace — or is every disappearance legitimate
(superseded with a visible trace, or moved with a link)?

LLM plumbing
------------
Reuses grade_wiki._build_judge_fn exactly: unified_llm.generate() wrapped in
asyncio.run() so the sync callable works from any call site.  No new LLM wiring.

Fate taxonomy (trace-or-justify, NOT naive monotonic)
------------------------------------------------------
  RETAINED      — claim present in after-wiki with same/equivalent meaning.
  SUPERSEDED    — claim's SUBJECT still addressed, but value/fact updated to a
                  newer value with a visible trace (e.g. "500, up from 100").
                  This is LEGITIMATE — NOT a loss.  Do NOT flag as SILENTLY_LOST.
  MOVED         — claim is on a different after-wiki page, ideally linked.
  SILENTLY_LOST — claim's subject/topic COMPLETELY absent from ALL after-wiki
                  pages; the topic is not mentioned in any form, old or new.

PASS = zero SILENTLY_LOST.

Usage (standalone CLI)
----------------------
    python eval/grade_claim_retention.py before_page.md after_wiki_dir/

Programmatic API
----------------
    from grade_claim_retention import grade_claim_retention
    result = grade_claim_retention(before_text, Path("wiki/"))
    print(result.report())
    assert result.passed   # zero SILENTLY_LOST
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# sys.path: make grade_wiki (and its pipeline dep) importable regardless of cwd
# ---------------------------------------------------------------------------
_EVAL = Path(__file__).resolve().parent
_REPO = _EVAL.parent
if str(_EVAL) not in sys.path:
    sys.path.insert(0, str(_EVAL))
if str(_REPO / "pipeline") not in sys.path:
    sys.path.insert(0, str(_REPO / "pipeline"))

# Reuse the established unified_llm wiring — do NOT duplicate it.
from grade_wiki import _build_judge_fn  # noqa: E402

# ---------------------------------------------------------------------------
# Judge prompt
# ---------------------------------------------------------------------------

_RETENTION_RUBRIC = """\
CLAIM RETENTION GRADER

Verify whether a wiki page re-write silently dropped any grounded claims.

DEFINITIONS
-----------
A GROUNDED CLAIM is a specific, verifiable assertion: a date, number, proper name,
version, procedure, or measurable property. Omit vague meta-sentences about the page
or the topic in general.

Fate taxonomy — classify EACH grounded claim found in the BEFORE page:

  RETAINED      — The claim is present in the after-wiki with the same or equivalent
                  meaning.  Quote the after-wiki sentence verbatim as evidence.

  SUPERSEDED    — The claim's SUBJECT/TOPIC is still addressed in the after-wiki, but
                  with an UPDATED or CONTRADICTING value that replaces the old one.
                  A visible trace exists: the new value itself, "up from X", or an
                  explicit note that the old value changed.
                  Quote the after-wiki sentence verbatim.

  MOVED         — The claim now appears on a DIFFERENT page in the after-wiki, ideally
                  with a wikilink [[Page Title]] pointing back to it.
                  Quote the after-wiki sentence verbatim.

  SILENTLY_LOST — The claim's subject/TOPIC is COMPLETELY ABSENT from ALL after-wiki
                  pages. Zero sentences address this topic in any form.
                  Note that you searched for the subject and found nothing.

CRITICAL DISTINCTION — SUPERSEDED vs SILENTLY_LOST
  If the after-wiki still discusses the claim's subject/topic — even with a different
  value — that is SUPERSEDED, NOT SILENTLY_LOST.
  Example: before says "supports up to 100 concurrent connections", after says
  "raised to 500, up from 100" — fate = SUPERSEDED (subject still addressed).
  Example: before says "first released March 2019 by Redway Systems", after has NO
  mention of founding date or founding company — fate = SILENTLY_LOST (topic absent).

INSTRUCTIONS
------------
1. Extract all GROUNDED claims from the BEFORE page (typically 4-8 claims).
   Quote each one verbatim.
2. For each claim, search ALL after-wiki pages for its subject/topic.
3. Classify the fate using the taxonomy above.
4. For RETAINED/SUPERSEDED/MOVED: provide a verbatim quote from the after-wiki as
   evidence so a human can spot-check (required — prevents hallucination).
5. For SILENTLY_LOST: state what subject you searched for and confirm absence.

Return ONLY valid JSON, no prose before or after:
{
  "claims": [
    {
      "claim_quote": "<exact verbatim quote from BEFORE page>",
      "fate": "RETAINED|SUPERSEDED|MOVED|SILENTLY_LOST",
      "evidence_quote_or_absence_note": "<verbatim from after-wiki OR absence note>"
    }
  ]
}

BEFORE PAGE (before re-write):
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gather_after_text(after_wiki_dir: Path) -> str:
    """Concatenate all .md files in after_wiki_dir, labelled by filename."""
    parts: list[str] = []
    for md in sorted(after_wiki_dir.glob("*.md")):
        parts.append(f"=== {md.name} ===\n")
        try:
            parts.append(md.read_text(encoding="utf-8"))
        except OSError:
            parts.append(f"[read error: {md.name}]\n")
        parts.append("\n")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Result container (mirrors GradeResult from grade_wiki.py)
# ---------------------------------------------------------------------------


class RetentionResult:
    """Outcome of grade_claim_retention() — PASS iff zero SILENTLY_LOST claims."""

    def __init__(self) -> None:
        # Each dict: {claim_quote, fate, evidence_quote_or_absence_note}
        self.claims: list[dict] = []
        self.error: str | None = None

    @property
    def passed(self) -> bool:
        """True iff no error and zero SILENTLY_LOST claims."""
        return self.error is None and not any(
            c.get("fate") == "SILENTLY_LOST" for c in self.claims
        )

    @property
    def silently_lost(self) -> list[dict]:
        """Claims whose fate is SILENTLY_LOST."""
        return [c for c in self.claims if c.get("fate") == "SILENTLY_LOST"]

    def report(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        lines = [f"[{verdict}] claim-retention"]
        if self.error:
            lines.append(f"  ERROR: {self.error}")
            return "\n".join(lines)
        fate_icons = {
            "RETAINED": "✓",
            "SUPERSEDED": "~",
            "MOVED": "→",
            "SILENTLY_LOST": "✗",
        }
        for c in self.claims:
            fate = c.get("fate", "?")
            icon = fate_icons.get(fate, "?")
            tag = f" [{fate}]" if fate == "SILENTLY_LOST" else f" [{fate}]"
            quote = c.get("claim_quote", "")[:80]
            evidence = c.get("evidence_quote_or_absence_note", "")[:120]
            lines.append(f"  {icon}{tag}  {quote}")
            lines.append(f"         evidence: {evidence}")
        if self.silently_lost:
            lines.append(
                f"\n  {len(self.silently_lost)} SILENTLY_LOST claim(s) — BUGs in re-write:"
            )
            for c in self.silently_lost:
                lines.append(f"    ✗ {c.get('claim_quote', '')}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public grader
# ---------------------------------------------------------------------------


def grade_claim_retention(
    before_page_text: str,
    after_wiki_dir: Path,
    judge_fn=None,
) -> RetentionResult:
    """Grade whether a page re-write silently dropped any grounded claims.

    Args:
        before_page_text: Full text of the wiki page BEFORE the re-write.
        after_wiki_dir:   Path to a directory containing wiki .md files AFTER
                          the re-write.  May contain multiple pages (the subject
                          page might have been split or merged).
        judge_fn:         Sync callable ``(prompt: str) -> str``.  If None,
                          builds one via _build_judge_fn() (requires unified_llm).

    Returns:
        RetentionResult.  result.passed is True iff zero SILENTLY_LOST claims.
        result.claims holds per-claim {claim_quote, fate, evidence_...} dicts
        with verbatim evidence quotes so a human can spot-check.
    """
    result = RetentionResult()

    if judge_fn is None:
        judge_fn = _build_judge_fn()
        if judge_fn is None:
            result.error = "unified_llm not importable; LLM judge unavailable"
            return result

    after_text = _gather_after_text(after_wiki_dir)
    if not after_text.strip():
        result.error = f"after_wiki_dir '{after_wiki_dir}' contains no .md files"
        return result

    prompt = (
        _RETENTION_RUBRIC
        + before_page_text[:8_000]
        + "\n\nAFTER WIKI (all pages after re-write, separated by filename):\n"
        + after_text[:12_000]
    )

    try:
        raw = judge_fn(prompt)
    except Exception as exc:
        result.error = f"judge_fn raised: {exc}"
        return result

    # Extract the JSON block; tolerate leading/trailing prose in the response.
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        result.error = f"judge returned no JSON block; raw response (first 500 chars):\n{raw[:500]}"
        return result

    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        result.error = f"JSON parse error: {exc}; raw (first 500 chars):\n{raw[:500]}"
        return result

    claims = data.get("claims", [])
    if not isinstance(claims, list):
        result.error = f"'claims' key is not a list; got {type(claims).__name__}"
        return result

    result.claims = [c for c in claims if isinstance(c, dict)]
    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Grade claim retention across a wiki re-write."
    )
    parser.add_argument(
        "before_page",
        type=Path,
        help="Path to the page .md file BEFORE the re-write.",
    )
    parser.add_argument(
        "after_wiki_dir",
        type=Path,
        help="Directory containing wiki .md files AFTER the re-write.",
    )
    args = parser.parse_args()

    if not args.before_page.is_file():
        print(f"ERROR: before_page not found: {args.before_page}", file=sys.stderr)
        sys.exit(2)
    if not args.after_wiki_dir.is_dir():
        print(
            f"ERROR: after_wiki_dir not found: {args.after_wiki_dir}", file=sys.stderr
        )
        sys.exit(2)

    before_text = args.before_page.read_text(encoding="utf-8")
    result = grade_claim_retention(before_text, args.after_wiki_dir)
    print(result.report())
    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    _cli()
