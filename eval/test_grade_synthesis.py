"""Calibration tests for grade_synthesis against the known-bad baseline wiki.

These tests pin the eval to the *concatenation baseline* (runs/known-bad/wiki,
paused at 87 converged articles) so that a future synthesis fix cannot silently
produce a passing score on the old output.

Baseline facts (measured 2026-06-12, run paused at 87 articles):
  total_source_labeled_sections : 60
      (spec estimated ~54; run grew from 65 → 87 articles between estimate and
      measurement, accounting for the ~11% difference)
  median_single_source_ratio    : 0.800
      (spec estimated ~1.0; the 5-6% of pages with genuinely cross-cited sections
      — e.g. flowise.md where every section cites both sources simultaneously —
      pull the median below 1.0.  The key signal is still FAIL: 76.5% of pages
      exceed the 0.7 threshold, far above the 20% gate.)
  deterministic_pass            : False  (G1 and G2 both violated)
  grade_synthesis result.passed : False  (G0 also fails — see note below)

NOTE on G0 / SynthesisScore = 0:
  no_duplicate_pages() was fixed (2026-06-12) to only flag slug-N.md when the
  matching base slug also exists — so version-named pages like deepseek-v3-2.md
  and year-named pages like ai-coding-trends-2026.md are no longer false positives.
  G0 may still fail on the known-bad baseline due to ledger integrity (run paused
  mid-stream), which zeros out the SynthesisScore structure-multiplier.
  The synthesis quality gates (G1, G2) still independently fail, which is the
  signal that matters.

These tests MUST continue to fail on the known-bad baseline and MUST pass once the
synthesis fix (integrate-by-theme prompts + updated CONVERGENCE_RUBRIC) lands.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Reach the wiki-weaver source the same way the eval scripts do.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "pipeline"))

from grade_wiki import grade_integration, grade_synthesis  # noqa: E402

# The known-bad baseline wiki — skip if unavailable (CI without fixture corpus).
_WIKI = _REPO / "runs" / "known-bad" / "wiki"
pytestmark = pytest.mark.skipif(
    not _WIKI.is_dir(),
    reason=f"known-bad baseline wiki not found at {_WIKI}",
)


@pytest.fixture(scope="module")
def integ():
    """grade_integration result — computed once for all tests in this module."""
    return grade_integration(_WIKI)


@pytest.fixture(scope="module")
def synthesis():
    """grade_synthesis result — deterministic-only (no judge_fn)."""
    return grade_synthesis(_WIKI, judge_fn=None)


# ---------------------------------------------------------------------------
# Primary calibration: the eval must flag this baseline as BAD
# ---------------------------------------------------------------------------


def test_deterministic_pass_is_false(integ):
    """The integration hard gate must fail on the concatenation baseline."""
    assert integ["deterministic_pass"] is False, (
        "deterministic_pass should be False on the known-bad baseline; "
        "if it's True the eval is no longer catching concatenation"
    )


def test_grade_synthesis_fails(synthesis):
    """grade_synthesis() must return a FAIL result on the known-bad baseline."""
    assert not synthesis.passed, (
        "grade_synthesis should FAIL on the known-bad baseline; "
        "if it passes the hard gate is broken"
    )


# ---------------------------------------------------------------------------
# Pin the metric numbers so regex regressions are caught
# ---------------------------------------------------------------------------


def test_source_labeled_sections_count(integ):
    """At least 50 source-labeled sections on multi-source pages (baseline: 60).

    A drop below 50 means the LABELED_HEADER regexes have been weakened and
    are no longer catching the concatenation pattern.
    """
    assert integ["total_source_labeled_sections"] >= 50, (
        f"expected >=50 source-labeled sections, "
        f"got {integ['total_source_labeled_sections']}; "
        "LABELED_HEADER regexes may have been weakened"
    )


def test_median_single_source_ratio(integ):
    """Median single-source section ratio is >= 0.70 on the known-bad baseline (0.800).

    This is a DIAGNOSTIC calibration — the ratio metric is no longer a hard gate
    (it cannot distinguish legit single-source coverage from artificial silos).
    The assertion pins the observed baseline value so we notice if the metric
    computation changes unexpectedly. It does NOT assert gate behaviour.
    """
    ratio = integ["median_single_source_ratio"]
    assert ratio is not None, "median_single_source_ratio should not be None"
    assert ratio >= 0.70, (
        f"expected median_single_source_ratio >= 0.70 on known-bad baseline, got {ratio:.3f}; "
        "the metric computation may have changed (this is a diagnostic calibration check)"
    )


def test_pct_over_0_7_is_high(integ):
    """Most multi-source pages have high single-source ratio on the known-bad baseline (76.5%).

    This is a DIAGNOSTIC calibration — pct_multi_pages_over_0_7 is no longer a
    hard gate (it cannot distinguish legit single-source coverage from artificial silos).
    The assertion pins the observed baseline value so we notice if the metric
    computation changes unexpectedly. It does NOT assert gate behaviour.
    """
    pct = integ["pct_multi_pages_over_0_7"]
    assert pct >= 0.60, (
        f"expected pct_multi_pages_over_0_7 >= 0.60 on known-bad baseline (76.5%), got {pct:.1%}; "
        "the metric computation may have changed (this is a diagnostic calibration check)"
    )


def test_g1_fails_explicitly(integ):
    """G1 gate: total_source_labeled_sections must be non-zero."""
    assert integ["total_source_labeled_sections"] > 0, (
        "G1 should fail: baseline has source-labeled sections that signal concatenation"
    )


def test_ratio_metrics_high_in_baseline(integ):
    """Ratio metrics are high on the known-bad baseline (diagnostic calibration).

    pct_multi_pages_over_0_7 should be well above 0.20 on the concatenation baseline.
    NOTE: these metrics are now DIAGNOSTICS ONLY — not hard gates. Demoted because
    ratio metrics cannot distinguish legit single-source coverage from artificial silos
    on uneven-coverage corpora. This test pins the observed baseline value as a
    calibration fact, not as a gate assertion.
    """
    assert integ["pct_multi_pages_over_0_7"] > 0.20, (
        f"diagnostic pct_over_0.7 check: baseline {integ['pct_multi_pages_over_0_7']:.1%} "
        "should be well above 0.20 on the known-bad baseline (this is a diagnostic calibration)"
    )
