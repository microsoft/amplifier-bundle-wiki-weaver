# pyright: reportMissingImports=false
"""Calibration tests for grade_claim_retention — static fixtures, no live ingest.

Three calibration cases prove the grader works in BOTH directions.

  BITE (calibrate-to-fail):
    After-page with canary C1 ("March 2019 by Redway Systems") completely deleted.
    Grader MUST classify C1 as SILENTLY_LOST.  result.passed MUST be False.
    If this test fails the eval cannot catch the bug we are hunting for.

  NO-FALSE-ALARM (calibrate-to-not-fire — the hard one):
    After-page where C2 ("100 concurrent connections") is superseded with a visible
    trace ("500 ... up from 100").  Grader MUST classify C2 as SUPERSEDED (NOT
    SILENTLY_LOST).  result.passed MUST be True.
    If this test fails the grader produces false alarms on every normal re-write,
    making it useless.

  CLEAN (normal-pass):
    After-page with canary C1 retained, C3 retained, C2 superseded.
    result.passed MUST be True (zero SILENTLY_LOST).

All tests skip if unified_llm is not importable — no LLM means no judge.
Skips count as green in pytest, preserving the 76-test baseline.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# sys.path setup — same pattern as the other eval test files
# ---------------------------------------------------------------------------
_EVAL = Path(__file__).resolve().parent
_REPO = _EVAL.parent
if str(_EVAL) not in sys.path:
    sys.path.insert(0, str(_EVAL))
if str(_REPO / "pipeline") not in sys.path:
    sys.path.insert(0, str(_REPO / "pipeline"))

from grade_claim_retention import _build_judge_fn, grade_claim_retention  # noqa: E402

# ---------------------------------------------------------------------------
# Skip all tests when LLM judge is unavailable (e.g. system pytest without
# unified_llm in its site-packages).  Skips are green; they do not break the
# existing 76-test baseline.
# ---------------------------------------------------------------------------
_JUDGE_FN = _build_judge_fn()

pytestmark = pytest.mark.skipif(
    _JUDGE_FN is None,
    reason="unified_llm not importable — LLM judge unavailable; skipping claim-retention tests",
)

# ---------------------------------------------------------------------------
# Static page fixtures — ground-truth known, no file I/O required
# ---------------------------------------------------------------------------

# The Beacon page as established by source-a only.
# Contains three grounded claims:
#   C1 (CANARY)    — "March 2019 by Redway Systems"   (standalone; source-b never mentions)
#   C2 (UPDATABLE) — "up to 100 concurrent connections" (source-b supersedes with 500)
#   C3 (STANDALONE) — "beacon.yaml … YAML files"       (source-b never mentions)
BEACON_BEFORE = """\
---
title: Beacon
sources: [1]
---

# Beacon

Beacon is an open-source, lightweight peer-to-peer networking library.

## Origin and History

Beacon was first released in March 2019 by Redway Systems as an open-source project
under the MIT license.

## Connection Limits

By default, each Beacon node supports up to 100 concurrent connections. This ceiling
can be raised via the `max_conns` configuration key.

## Configuration

Beacon configuration is defined in YAML files. The primary config file is `beacon.yaml`,
placed at the project root. All runtime parameters are read from this file at startup.

## Routing

Beacon uses a consistent-hash ring for peer discovery and routes messages by hashing
the destination service ID.
"""

# After-clean: a correct re-write.
#   C1 RETAINED  — "March 2019 by Redway Systems" present in ## Origin and History.
#   C2 SUPERSEDED — "500 … up from 100" (visible trace of the old value).
#   C3 RETAINED  — "beacon.yaml … YAML files" present.
#   C4/C5 ADDED  — TLS and plugin architecture from source-b.
BEACON_AFTER_CLEAN = """\
---
title: Beacon
sources: [1, 2]
---

# Beacon

Beacon is an open-source, lightweight peer-to-peer networking library.

## Origin and History

Beacon was first released in March 2019 by Redway Systems as an open-source project
under the MIT license. The v2.0 release followed in October 2022.

## Connection Limits

Beacon v2.0 raised the default concurrent connection limit to 500 connections per node —
up from 100. Teams running high-fan-out topologies no longer need to tune `max_conns`
for typical workloads.

## Configuration

Beacon configuration is defined in YAML files. The primary config file is `beacon.yaml`,
placed at the project root. All runtime parameters are read from this file at startup.

## TLS Encryption

Beacon v2.0 introduces TLS 1.3 support for peer-to-peer connections, enabled via the
`tls.enabled: true` flag in the configuration.

## Plugin Architecture

Beacon v2.0 ships a plugin system for custom protocol handlers, registered via the
`BeaconPlugin` interface from the `plugins/` directory.

## Routing

Beacon uses a consistent-hash ring for peer discovery and routes messages by hashing
the destination service ID.
"""

# After-bite: C1 (founding history) SILENTLY DELETED.
#   No mention of "March 2019", "Redway Systems", or any founding/origin information.
#   The "## Origin and History" section is completely gone.
#   C2 superseded (visible trace present).
#   C3 retained.
BEACON_AFTER_BITE = """\
---
title: Beacon
sources: [1, 2]
---

# Beacon

Beacon is an open-source, lightweight peer-to-peer networking library.

## Connection Limits

Beacon v2.0 raised the default concurrent connection limit to 500 connections per node —
up from 100. Teams running high-fan-out topologies no longer need to tune `max_conns`
for typical workloads.

## Configuration

Beacon configuration is defined in YAML files. The primary config file is `beacon.yaml`,
placed at the project root. All runtime parameters are read from this file at startup.

## TLS Encryption

Beacon v2.0 introduces TLS 1.3 support for peer-to-peer connections, enabled via the
`tls.enabled: true` flag in the configuration.

## Plugin Architecture

Beacon v2.0 ships a plugin system for custom protocol handlers, registered via the
`BeaconPlugin` interface from the `plugins/` directory.

## Routing

Beacon uses a consistent-hash ring for peer discovery and routes messages by hashing
the destination service ID.
"""

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def judge_fn():
    """Return the shared LLM judge callable (built once per test module)."""
    return _JUDGE_FN


# ---------------------------------------------------------------------------
# Test 1 — BITE: canary silently deleted → must be flagged SILENTLY_LOST
# ---------------------------------------------------------------------------


def test_bite_canary_silently_lost(judge_fn, tmp_path):
    """Grader must flag C1 as SILENTLY_LOST when it is deleted from the after-page.

    BEACON_AFTER_BITE has zero mention of 'March 2019', 'Redway Systems', or any
    founding/origin information.  The grader must detect this absence.

    If this test FAILS it means the grader cannot catch silent drops — the entire
    eval is broken and Finding 1 cannot be verified.
    """
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    (wiki_dir / "beacon.md").write_text(BEACON_AFTER_BITE, encoding="utf-8")

    result = grade_claim_retention(BEACON_BEFORE, wiki_dir, judge_fn=judge_fn)

    assert result.error is None, f"Grader returned an error: {result.error}"
    assert result.claims, "Grader must extract at least one claim from the before page"

    # ── Gate: at least one claim must be SILENTLY_LOST ────────────────────
    lost = result.silently_lost
    assert lost, (
        "BITE calibration FAILED: grader found zero SILENTLY_LOST claims, "
        "but the canary ('March 2019 by Redway Systems') is absent from the "
        "after-wiki entirely.  Grader cannot catch silent drops; it is miscalibrated.\n"
        f"Claims returned: {[c.get('claim_quote', '')[:80] for c in result.claims]}"
    )

    # ── Gate: the canary claim specifically must be among the lost ─────────
    canary_keywords = {"2019", "redway", "march"}
    canary_lost = [
        c
        for c in lost
        if any(kw in c.get("claim_quote", "").lower() for kw in canary_keywords)
    ]
    assert canary_lost, (
        "BITE calibration: SILENTLY_LOST claims found, but none matched C1 (canary). "
        f"Lost claims: {[c.get('claim_quote', '')[:80] for c in lost]}.  "
        "Check that the grader is extracting C1 from the before page."
    )

    # ── Gate: result.passed must be False (non-zero SILENTLY_LOST) ─────────
    assert not result.passed, (
        "Grader returned passed=True despite SILENTLY_LOST claims — logic error in "
        "RetentionResult.passed."
    )


# ---------------------------------------------------------------------------
# Test 2 — NO FALSE ALARM: superseded claim must NOT be SILENTLY_LOST
# ---------------------------------------------------------------------------


def test_no_false_alarm_on_supersession(judge_fn, tmp_path):
    """Superseded claim C2 (100 → 500 connections) must NOT be classified SILENTLY_LOST.

    BEACON_AFTER_CLEAN says '500 connections per node — up from 100'.  The subject
    (connection limit) is still present with a new value and a visible trace
    ('up from 100').  The grader must classify this as SUPERSEDED, not SILENTLY_LOST.

    If this test FAILS the grader cannot distinguish a legitimate update from a silent
    drop — it will produce false alarms on every normal re-write, making it useless.
    """
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    (wiki_dir / "beacon.md").write_text(BEACON_AFTER_CLEAN, encoding="utf-8")

    result = grade_claim_retention(BEACON_BEFORE, wiki_dir, judge_fn=judge_fn)

    assert result.error is None, f"Grader returned an error: {result.error}"
    assert result.claims, "Grader must extract at least one claim from the before page"

    # ── Gate: the '100 concurrent connections' claim must NOT be SILENTLY_LOST ─
    conn_lost = [
        c
        for c in result.silently_lost
        if "100" in c.get("claim_quote", "")
        or "concurrent" in c.get("claim_quote", "").lower()
    ]
    assert not conn_lost, (
        "NO-FALSE-ALARM calibration FAILED: grader classified the '100 concurrent "
        "connections' claim as SILENTLY_LOST even though the after-wiki says "
        "'500 connections per node — up from 100'.  The grader cannot distinguish "
        "SUPERSEDED from SILENTLY_LOST and will produce false alarms on normal re-writes.\n"
        f"Wrongly lost claims: {[c.get('claim_quote', '')[:80] for c in conn_lost]}"
    )


# ---------------------------------------------------------------------------
# Test 3 — CLEAN: all claims retained or superseded → PASS
# ---------------------------------------------------------------------------


def test_clean_page_passes(judge_fn, tmp_path):
    """When all claims are retained or superseded, result.passed must be True.

    BEACON_AFTER_CLEAN has:
      C1 retained  — 'March 2019 by Redway Systems' in ## Origin and History
      C2 superseded — '500 ... up from 100' (visible trace)
      C3 retained  — 'beacon.yaml … YAML files' present
      C4/C5 added  — new facts (TLS, plugin architecture), not a loss

    result.passed must be True — zero SILENTLY_LOST claims.
    """
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    (wiki_dir / "beacon.md").write_text(BEACON_AFTER_CLEAN, encoding="utf-8")

    result = grade_claim_retention(BEACON_BEFORE, wiki_dir, judge_fn=judge_fn)

    assert result.error is None, f"Grader returned an error: {result.error}"
    assert result.claims, "Grader must extract at least one claim from the before page"
    assert result.passed, (
        "CLEAN calibration FAILED: grader returned FAIL on a page where all claims "
        "are retained or superseded.  "
        f"SILENTLY_LOST claims reported: "
        f"{[c.get('claim_quote', '')[:80] for c in result.silently_lost]}"
    )
