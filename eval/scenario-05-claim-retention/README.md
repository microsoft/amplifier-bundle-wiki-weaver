# Scenario 05 — Claim Retention on Re-Write

Tests whether Phase A's per-section re-write (triggered when a new source touches an
existing page) ever **silently drops** a previously-grounded claim — i.e. the claim
vanishes with zero trace, not even a mention that the topic changed.

## Subject

**Beacon** — a fictional open-source peer-to-peer networking framework. Chosen because
the name is unique enough to land on a single wiki page (no topic overlap with real
corpus pages), so both sources force a re-write of the same `beacon.md` page.

## Sources

| File | Content |
|------|---------|
| `source-a.md` | Beacon v1 introduction — establishes the page with grounded claims C1–C3. |
| `source-b.md` | Beacon v2.0 release notes — supersedes C2, adds C4/C5, never mentions C1 or C3. |

## Ground-Truth Claim Fates

| ID | Claim (verbatim from `source-a.md`) | Expected fate after source-b re-write |
|----|--------------------------------------|---------------------------------------|
| **C1 — CANARY** | "Beacon was first released in March 2019 by Redway Systems as an open-source project under the MIT license." | **RETAINED** — source-b never mentions founding date or company; C1 must survive verbatim or paraphrased on the re-written page. |
| **C2 — UPDATABLE** | "By default, each Beacon node supports up to 100 concurrent connections." | **SUPERSEDED** — source-b raises the limit to 500 ("a fivefold increase over the previous default"). The after-page should state the new limit and may reference the old one ("up from 100"). The subject (connection limit) is still addressed with a visible trace — this is NOT a loss. |
| **C3 — STANDALONE** | "Beacon configuration is defined in YAML files. The primary config file is `beacon.yaml`, which must be placed at the project root." | **RETAINED** — source-b says nothing about configuration format; C3 must survive. |
| **C4 — NEW** | *(not in source-a)* "Beacon v2.0 introduces TLS 1.3 support …" | New addition from source-b. Not a before-claim; the grader does not score it. |
| **C5 — NEW** | *(not in source-a)* "Beacon v2.0 ships a plugin system …" | New addition from source-b. Not a before-claim; the grader does not score it. |

## What a Correct Re-Write Looks Like

A passing re-write must:
1. Retain C1 — the founding history section should survive even though source-b doesn't
   mention it.
2. Address C2's subject — the connection-limit topic must still appear, updated to 500.
   The grader accepts "up from 100" or any phrasing that makes C2's supersession visible.
3. Retain C3 — the YAML configuration section must survive.
4. Include C4 and C5 — new facts from source-b (TLS, plugin architecture).

## What a FAILING Re-Write Looks Like

A re-write FAILS if C1 or C3 disappear entirely — the topics "Beacon founding / history"
and "Beacon YAML configuration" have no presence whatsoever in the after-page.

## Running the Eval

After you have ingested source-a, snapshotted the page, ingested source-b, and
snapshotted again:

```bash
# Grade claim retention on the Beacon page
python eval/grade_claim_retention.py \
  /path/to/before_snapshot/beacon.md \
  /path/to/after_wiki/
```

The grader reads all `.md` files in the after_wiki dir, extracts grounded claims from
the before-page, classifies each claim's fate, and exits non-zero if any claim is
`SILENTLY_LOST`.

## Calibration Tests

`eval/test_claim_retention.py` runs three calibration cases against static page
fixtures (no live ingest required):

| Test | Fixture | Expected grader verdict |
|------|---------|------------------------|
| `test_bite_canary_silently_lost` | After-page with C1 deleted | FAIL — canary is `SILENTLY_LOST` |
| `test_no_false_alarm_on_supersession` | After-page with C2 superseded ("up from 100") | PASS — C2 is `SUPERSEDED`, NOT `SILENTLY_LOST` |
| `test_clean_page_passes` | After-page with C1/C3 retained, C2 superseded | PASS — zero `SILENTLY_LOST` |

The no-false-alarm test is the distinguishing one: it proves the grader can tell a
legitimate supersession from a silent drop. Without it, the grader would produce false
alarms on every normal re-write.
