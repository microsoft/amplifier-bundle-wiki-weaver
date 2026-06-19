"""Unit tests for Fix 2: frontmatter provenance reading in source registration.

Tests:
  - _read_source_frontmatter() parses author/url/date from YAML frontmatter
  - _assign_source_id() stores provenance into .sources.json
  - Missing fields handled gracefully (no crash, no fabrication)

These are deterministic — no network, no LLM, no pipeline run required.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Insert the repo root so we can import wiki_weaver.wiki_weaver without installing.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from wiki_weaver.wiki_weaver import (  # noqa: E402
    REGISTRY_NAME,
    _assign_source_id,
    _read_source_frontmatter,
)

# ---------------------------------------------------------------------------
# Test fixtures (source article markdown content)
# ---------------------------------------------------------------------------

# Full frontmatter with all three provenance fields
_FM_FULL = """\
---
title: "Sample Article About LLMs"
author: "Jane Doe"
source: "https://example.com/sample-article-abc123"
date: "2024-05-01"
---

# Sample Article About LLMs

The body text of the article follows here.
"""

# Frontmatter with url: field instead of source: (alternative naming)
_FM_URL_KEY = """\
---
title: "Another Article"
author: "Bob Smith"
url: "https://example.com/another-article"
---

Body text.
"""

# Frontmatter with only author — no URL or date
_FM_AUTHOR_ONLY = """\
---
title: "Partial Metadata"
author: "Alice Chen"
---

Body text.
"""

# No frontmatter at all
_FM_NONE = """\
# No Frontmatter Article

Just a plain markdown file with no YAML header.
"""

# Frontmatter present but empty / incomplete
_FM_EMPTY_BLOCK = """\
---
---

Body text with no frontmatter fields.
"""

# Real-world sample matching the corpus archive files
_FM_REAL_WORLD = """\
---
title: "10 AI Agent Tools That Are Reshaping the Industry in 2025"
author: "Murat Aslan"
source: "https://blog.stackademic.com/10-ai-agent-tools-abc123"
---

# 10 AI Agent Tools That Are Reshaping the Industry in 2025

Article body here.
"""


# ---------------------------------------------------------------------------
# Tests for _read_source_frontmatter()
# ---------------------------------------------------------------------------


class TestReadSourceFrontmatter:
    """Unit tests for the frontmatter parser."""

    def test_reads_all_three_fields(self, tmp_path):
        src = tmp_path / "article.md"
        src.write_text(_FM_FULL, encoding="utf-8")
        result = _read_source_frontmatter(src)
        assert result["author"] == "Jane Doe"
        assert result["url"] == "https://example.com/sample-article-abc123"
        assert result["date"] == "2024-05-01"

    def test_url_key_accepted(self, tmp_path):
        """source: and url: are both accepted as the URL field."""
        src = tmp_path / "article.md"
        src.write_text(_FM_URL_KEY, encoding="utf-8")
        result = _read_source_frontmatter(src)
        assert result["author"] == "Bob Smith"
        assert result["url"] == "https://example.com/another-article"
        assert result["date"] is None

    def test_partial_frontmatter_no_crash(self, tmp_path):
        src = tmp_path / "article.md"
        src.write_text(_FM_AUTHOR_ONLY, encoding="utf-8")
        result = _read_source_frontmatter(src)
        assert result["author"] == "Alice Chen"
        assert result["url"] is None
        assert result["date"] is None

    def test_no_frontmatter_returns_nones(self, tmp_path):
        src = tmp_path / "article.md"
        src.write_text(_FM_NONE, encoding="utf-8")
        result = _read_source_frontmatter(src)
        assert result == {"author": None, "url": None, "date": None}

    def test_empty_frontmatter_block_returns_nones(self, tmp_path):
        src = tmp_path / "article.md"
        src.write_text(_FM_EMPTY_BLOCK, encoding="utf-8")
        result = _read_source_frontmatter(src)
        assert result == {"author": None, "url": None, "date": None}

    def test_real_world_corpus_frontmatter(self, tmp_path):
        src = tmp_path / "article.md"
        src.write_text(_FM_REAL_WORLD, encoding="utf-8")
        result = _read_source_frontmatter(src)
        assert result["author"] == "Murat Aslan"
        assert "stackademic" in result["url"]
        assert result["date"] is None

    def test_missing_file_returns_nones(self, tmp_path):
        """A non-existent file should return all-None without raising."""
        result = _read_source_frontmatter(tmp_path / "nonexistent.md")
        assert result == {"author": None, "url": None, "date": None}


# ---------------------------------------------------------------------------
# Tests for _assign_source_id() provenance storage
# ---------------------------------------------------------------------------


def _make_wiki(tmp_path: Path) -> Path:
    """Scaffold a minimal wiki directory structure."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "_inbox").mkdir()
    (wiki / "_archive").mkdir()
    (wiki / ".ai" / "feedback").mkdir(parents=True)
    return wiki


class TestAssignSourceIdProvenance:
    """Test that _assign_source_id stores author/url/date into .sources.json."""

    def test_full_provenance_stored(self, tmp_path):
        """Full frontmatter → all three fields in registry entry."""
        wiki = _make_wiki(tmp_path)
        src = tmp_path / "article.md"
        src.write_text(_FM_FULL, encoding="utf-8")

        entry, is_new = _assign_source_id(wiki, src)

        assert is_new
        assert entry["author"] == "Jane Doe"
        assert entry["url"] == "https://example.com/sample-article-abc123"
        assert entry.get("date") == "2024-05-01"
        # Verify persisted to .sources.json
        data = json.loads((wiki / REGISTRY_NAME).read_text(encoding="utf-8"))
        saved = data["sources"][0]
        assert saved["author"] == "Jane Doe"
        assert saved["url"] == "https://example.com/sample-article-abc123"
        assert saved.get("date") == "2024-05-01"

    def test_no_frontmatter_no_crash(self, tmp_path):
        """Source with no frontmatter: entry created, no author/url/date keys."""
        wiki = _make_wiki(tmp_path)
        src = tmp_path / "article.md"
        src.write_text(_FM_NONE, encoding="utf-8")

        entry, is_new = _assign_source_id(wiki, src)

        assert is_new
        # Provenance keys should NOT be present (not stored as None)
        assert "author" not in entry
        assert "url" not in entry
        assert "date" not in entry
        # Registry valid
        data = json.loads((wiki / REGISTRY_NAME).read_text(encoding="utf-8"))
        assert len(data["sources"]) == 1
        assert data["sources"][0]["id"] == 1
        assert "author" not in data["sources"][0]

    def test_existing_hash_returns_existing_entry(self, tmp_path):
        """Duplicate hash: second call returns the cached entry unchanged."""
        wiki = _make_wiki(tmp_path)
        src = tmp_path / "article.md"
        src.write_text(_FM_FULL, encoding="utf-8")

        entry1, is_new1 = _assign_source_id(wiki, src)
        assert is_new1

        entry2, is_new2 = _assign_source_id(wiki, src)
        assert not is_new2
        assert entry2["id"] == entry1["id"]

    def test_two_sources_get_distinct_ids_and_provenance(self, tmp_path):
        """Two different sources each get their own id and provenance."""
        wiki = _make_wiki(tmp_path)

        src1 = tmp_path / "a.md"
        src1.write_text(_FM_FULL, encoding="utf-8")
        src2 = tmp_path / "b.md"
        src2.write_text(_FM_REAL_WORLD, encoding="utf-8")

        e1, _ = _assign_source_id(wiki, src1)
        e2, _ = _assign_source_id(wiki, src2)

        assert e1["id"] != e2["id"]
        assert e1["author"] == "Jane Doe"
        assert e2["author"] == "Murat Aslan"

        data = json.loads((wiki / REGISTRY_NAME).read_text(encoding="utf-8"))
        assert len(data["sources"]) == 2

    def test_grade_provenance_would_pass(self, tmp_path):
        """End-to-end: a registry with all entries having author+url passes
        grade_provenance >= 80% threshold.

        This does not run the full pipeline — it just verifies that a registry
        produced by _assign_source_id for sources with full frontmatter would
        satisfy the grade_provenance hard gate.
        """
        import importlib.util  # noqa: PLC0415

        grade_spec = importlib.util.spec_from_file_location(
            "grade_wiki",
            _REPO / "eval" / "grade_wiki.py",
        )
        grade_mod = importlib.util.module_from_spec(grade_spec)  # type: ignore
        sys.modules["grade_wiki"] = grade_mod
        grade_spec.loader.exec_module(grade_mod)  # type: ignore
        grade_provenance = grade_mod.grade_provenance

        wiki = _make_wiki(tmp_path)

        articles = [
            (_FM_FULL, "a.md"),
            (_FM_REAL_WORLD, "b.md"),
            (_FM_URL_KEY, "c.md"),
        ]
        for content, fname in articles:
            src = tmp_path / fname
            src.write_text(content, encoding="utf-8")
            _assign_source_id(wiki, src)

        result = grade_provenance(wiki)
        assert result.passed, (
            "grade_provenance should PASS when all entries have author+url; "
            f"failures: {result.failures}"
        )
