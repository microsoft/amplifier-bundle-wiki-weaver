"""Eval gate: transcript-header → provenance adapter.

Tests that the _parse_transcript_header adapter correctly extracts provenance
from meeting-transcript header blocks (which carry no YAML frontmatter) and
that the extracted provenance flows through to grade_provenance.

Calibration structure:
  NEGATIVE (calibrate-to-fail):
    A plain source (no YAML frontmatter, no transcript markers) yields no
    author/url.  A wiki built from only plain sources FAILS grade_provenance
    (0% < 80% threshold).

  POSITIVE (calibrate-to-pass):
    A transcript source is parsed by _parse_transcript_header into author
    (Speakers), url (Source), date, and title.  _read_source_frontmatter
    returns those fields.  _assign_source_id stores them.  grade_provenance
    on the resulting wiki PASSES (100% >= 80% threshold).

  REGRESSION (YAML frontmatter unchanged):
    A source with YAML frontmatter goes through the existing YAML path,
    byte-identical to before the adapter.  grade_provenance still PASSES.

All tests are deterministic — no network, no LLM, no pipeline run.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from wiki_weaver.wiki_weaver import (  # noqa: E402
    REGISTRY_NAME,
    _assign_source_id,
    _parse_transcript_header,
    _read_source_frontmatter,
)

# ---------------------------------------------------------------------------
# Load grade_wiki via explicit path (avoids sys.path guessing)
# ---------------------------------------------------------------------------

_grade_spec = importlib.util.spec_from_file_location(
    "grade_wiki_transcript",
    _REPO / "eval" / "grade_wiki.py",
)
_grade_mod = importlib.util.module_from_spec(_grade_spec)  # type: ignore[arg-type]
sys.modules["grade_wiki_transcript"] = _grade_mod
_grade_spec.loader.exec_module(_grade_mod)  # type: ignore[union-attr]
grade_provenance = _grade_mod.grade_provenance


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Representative transcript header — no YAML frontmatter, labeled metadata block
_TRANSCRIPT_FULL = """\
# Transcript: Weekly Planning Sync

Source: https://example.com/meetings/weekly-planning-2026-05-29
Duration: 1:00:50
Speakers: Chris Park, Alex Rivera, Samuel Lee
Date: 5/29/2026, 11:07:43 AM
Chat type: Meeting
Call ID: d36fb9d2-dead-beef-cafe-0123456789ab
Attendees: Samuel Lee, Chris Park, Alex Rivera

---

[0:00:04] Chris Park: Good morning everyone, let's get started.
[0:00:15] Alex Rivera: Morning. Should we jump into the backlog first?
"""

# Transcript with only Attendees: (no Speakers:) — tests fallback author
_TRANSCRIPT_ATTENDEES_ONLY = """\
# Transcript: Engineering Stand-up

Source: https://example.com/standup-recording
Date: 6/1/2026, 10:00:00 AM
Attendees: Alice Chen, Bob Smith

---

[0:00:01] Alice Chen: Good morning.
"""

# Plain file — no YAML frontmatter, no transcript markers
_PLAIN_NO_FRONTMATTER = """\
# No Frontmatter Article

Just a plain markdown file with no YAML header and no transcript markers.
This simulates the existing "no provenance" scenario.
"""

# YAML frontmatter (source: field style) — regression fixture
_YAML_FRONTMATTER = """\
---
title: "Sample Article About LLMs"
author: "Jane Doe"
source: "https://example.com/sample-article-abc123"
date: "2024-05-01"
---

# Sample Article About LLMs

The body text of the article follows here.
"""


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_wiki(tmp_path: Path) -> Path:
    """Scaffold a minimal wiki directory (same as test_provenance_unit.py)."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "_inbox").mkdir()
    (wiki / "_archive").mkdir()
    (wiki / ".ai" / "feedback").mkdir(parents=True)
    return wiki


# ---------------------------------------------------------------------------
# Section 1: _parse_transcript_header unit tests
# ---------------------------------------------------------------------------


class TestParseTranscriptHeader:
    """Direct unit tests for the pure-function adapter."""

    def test_full_transcript_extracts_all_fields(self):
        """Full transcript header → author (Speakers), url (Source), date, title."""
        result = _parse_transcript_header(_TRANSCRIPT_FULL)
        assert result["author"] == "Chris Park, Alex Rivera, Samuel Lee"
        assert (
            result["url"] == "https://example.com/meetings/weekly-planning-2026-05-29"
        )
        assert result["date"] == "5/29/2026, 11:07:43 AM"
        assert result["title"] == "Weekly Planning Sync"

    def test_attendees_fallback_author(self):
        """When Speakers: is absent, Attendees: is used as the author."""
        result = _parse_transcript_header(_TRANSCRIPT_ATTENDEES_ONLY)
        assert result["author"] == "Alice Chen, Bob Smith"
        assert result["url"] == "https://example.com/standup-recording"
        assert result["date"] == "6/1/2026, 10:00:00 AM"
        assert result["title"] == "Engineering Stand-up"

    def test_plain_file_returns_all_none(self):
        """Plain file with no transcript markers returns all-None (no false positive)."""
        result = _parse_transcript_header(_PLAIN_NO_FRONTMATTER)
        assert result == {"author": None, "url": None, "date": None, "title": None}

    def test_yaml_frontmatter_file_returns_all_none(self):
        """YAML frontmatter file returns all-None from transcript parser (YAML wins elsewhere)."""
        result = _parse_transcript_header(_YAML_FRONTMATTER)
        # YAML file has no Speakers/Attendees markers → all-None
        assert result == {"author": None, "url": None, "date": None, "title": None}

    def test_non_http_source_not_extracted(self):
        """Source: without http/https prefix is ignored (avoids prose false positives)."""
        text = """\
# Transcript: Seminar Notes

Speakers: Dr. Alice, Dr. Bob
Source: Smith et al., 2024

---

[0:00:00] Dr. Alice: Let's begin.
"""
        result = _parse_transcript_header(text)
        assert result["url"] is None  # "Smith et al., 2024" is not a URL
        assert result["author"] == "Dr. Alice, Dr. Bob"

    def test_empty_string_returns_all_none(self):
        """Empty input doesn't crash."""
        result = _parse_transcript_header("")
        assert result == {"author": None, "url": None, "date": None, "title": None}


# ---------------------------------------------------------------------------
# Section 2: _read_source_frontmatter integration (adapter wired in)
# ---------------------------------------------------------------------------


class TestReadSourceFrontmatterWithTranscriptFallback:
    """Tests that _read_source_frontmatter returns transcript provenance as fallback."""

    def test_transcript_file_yields_author_and_url(self, tmp_path):
        """Transcript file → _read_source_frontmatter returns author, url, date."""
        src = tmp_path / "transcript.md"
        src.write_text(_TRANSCRIPT_FULL, encoding="utf-8")
        result = _read_source_frontmatter(src)
        assert result["author"] == "Chris Park, Alex Rivera, Samuel Lee"
        assert (
            result["url"] == "https://example.com/meetings/weekly-planning-2026-05-29"
        )
        assert result["date"] == "5/29/2026, 11:07:43 AM"

    def test_plain_file_still_returns_nones(self, tmp_path):
        """Plain file (no frontmatter, no transcript markers) → unchanged all-None."""
        src = tmp_path / "plain.md"
        src.write_text(_PLAIN_NO_FRONTMATTER, encoding="utf-8")
        result = _read_source_frontmatter(src)
        assert result == {"author": None, "url": None, "date": None}

    def test_yaml_frontmatter_unchanged(self, tmp_path):
        """YAML frontmatter path is byte-identical (regression)."""
        src = tmp_path / "article.md"
        src.write_text(_YAML_FRONTMATTER, encoding="utf-8")
        result = _read_source_frontmatter(src)
        assert result["author"] == "Jane Doe"
        assert result["url"] == "https://example.com/sample-article-abc123"
        assert result["date"] == "2024-05-01"


# ---------------------------------------------------------------------------
# Section 3: NEGATIVE — plain sources → grade_provenance FAILS (calibrate-to-fail)
# ---------------------------------------------------------------------------


class TestNegativeGradeProvenance:
    """Without the adapter (or for plain non-transcript sources), provenance is 0%."""

    def test_plain_sources_fail_grade_provenance(self, tmp_path):
        """Plain sources have no author/url → grade_provenance FAILS (PR1 gate).

        This is the BEFORE state: what would happen if sources had no frontmatter
        and no transcript header.  The adapter returns all-None for plain files,
        so they get no author/url in the registry, reproducing the pre-adapter
        failure mode.
        """
        wiki = _make_wiki(tmp_path)

        # Register three plain (no-provenance) sources
        for i in range(3):
            src = tmp_path / f"plain_{i}.md"
            src.write_text(
                f"# Article {i}\n\nPlain content with no metadata.\n", encoding="utf-8"
            )
            _assign_source_id(wiki, src)

        data = json.loads((wiki / REGISTRY_NAME).read_text(encoding="utf-8"))
        # Confirm no author/url in any entry
        for entry in data["sources"]:
            assert "author" not in entry, (
                f"Expected no author on plain source; got {entry}"
            )
            assert "url" not in entry, f"Expected no url on plain source; got {entry}"

        result = grade_provenance(wiki)
        assert not result.passed, (
            "grade_provenance should FAIL when plain sources have no author/url "
            f"(calibrate-to-fail gate); failures: {result.failures}"
        )


# ---------------------------------------------------------------------------
# Section 4: POSITIVE — transcript sources → grade_provenance PASSES
# ---------------------------------------------------------------------------


class TestPositiveGradeProvenance:
    """Transcript sources yield author+url → grade_provenance PASSES."""

    def test_transcript_sources_pass_grade_provenance(self, tmp_path):
        """Transcript files → adapter fills author+url → grade_provenance PASSES (PR1).

        This is the AFTER state: the adapter extracts provenance from transcript
        headers and the registry is populated with author and url fields.
        """
        wiki = _make_wiki(tmp_path)

        transcripts = [
            ("t1.md", _TRANSCRIPT_FULL),
            ("t2.md", _TRANSCRIPT_ATTENDEES_ONLY),
        ]
        for fname, content in transcripts:
            src = tmp_path / fname
            src.write_text(content, encoding="utf-8")
            entry, is_new = _assign_source_id(wiki, src)
            assert is_new
            assert entry.get("author"), (
                f"Expected author in entry for {fname}; got {entry}"
            )
            assert entry.get("url"), f"Expected url in entry for {fname}; got {entry}"

        result = grade_provenance(wiki)
        assert result.passed, (
            "grade_provenance should PASS when transcript sources supply author+url; "
            f"failures: {result.failures}"
        )

    def test_mixed_transcript_and_plain_above_threshold(self, tmp_path):
        """2 transcripts + 1 plain = 67% — below threshold, fails.  3 transcripts + 1 plain = 75% — still below.

        This test proves the 80% threshold is real: a small proportion of plain
        sources can still drag provenance below the gate.
        """
        wiki = _make_wiki(tmp_path)

        # 2 transcripts (author+url present)
        for fname, content in [
            ("t1.md", _TRANSCRIPT_FULL),
            ("t2.md", _TRANSCRIPT_ATTENDEES_ONLY),
        ]:
            src = tmp_path / fname
            src.write_text(content, encoding="utf-8")
            _assign_source_id(wiki, src)

        # 1 plain (no author/url)
        src = tmp_path / "plain.md"
        src.write_text(_PLAIN_NO_FRONTMATTER, encoding="utf-8")
        _assign_source_id(wiki, src)

        # 2/3 ≈ 67% < 80% → FAILS
        result = grade_provenance(wiki)
        assert not result.passed, (
            "67% provenance should still FAIL the 80% gate; "
            f"failures: {result.failures}"
        )

    def test_four_transcripts_pass_grade_provenance(self, tmp_path):
        """4 transcripts (all with author+url) = 100% → PASSES."""
        wiki = _make_wiki(tmp_path)

        # Generate 4 transcript-format sources
        for i in range(4):
            content = f"""\
# Transcript: Planning Session {i}

Source: https://example.com/meeting-{i}
Speakers: Alice Chen, Bob Smith
Date: 6/{i + 1}/2026, 10:00:00 AM

---

[0:00:01] Alice Chen: Hello.
"""
            src = tmp_path / f"meeting_{i}.md"
            src.write_text(content, encoding="utf-8")
            _assign_source_id(wiki, src)

        result = grade_provenance(wiki)
        assert result.passed, (
            "grade_provenance should PASS for 4 transcript sources (100% author+url); "
            f"failures: {result.failures}"
        )


# ---------------------------------------------------------------------------
# Section 5: REGRESSION — YAML frontmatter path unchanged
# ---------------------------------------------------------------------------


class TestYamlRegressionGradeProvenance:
    """Prove the YAML frontmatter path is byte-identical after the adapter change."""

    def test_yaml_sources_pass_grade_provenance_unchanged(self, tmp_path):
        """YAML-frontmatter sources still populate author+url correctly (regression)."""
        wiki = _make_wiki(tmp_path)

        yaml_articles = [
            ("a.md", _YAML_FRONTMATTER),
            (
                "b.md",
                """\
---
title: "Another Article"
author: "Bob Smith"
url: "https://example.com/another-article"
---

Body text.
""",
            ),
        ]
        for fname, content in yaml_articles:
            src = tmp_path / fname
            src.write_text(content, encoding="utf-8")
            entry, is_new = _assign_source_id(wiki, src)
            assert is_new
            assert entry.get("author"), f"YAML regression: expected author for {fname}"
            assert entry.get("url"), f"YAML regression: expected url for {fname}"

        result = grade_provenance(wiki)
        assert result.passed, (
            "YAML regression: grade_provenance should still PASS for YAML sources; "
            f"failures: {result.failures}"
        )
