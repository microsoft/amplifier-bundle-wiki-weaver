"""Phase D regression tests — default-policy fallback and byte-identical guarantee.

Three test groups (all deterministic — no LLM, no network, no pipeline run):

  1. load_policy defaults
     Verify that load_policy on a wiki with NO policy/ dir and NO wiki.config.yaml
     returns the built-in defaults for all fields.

  2. Golden DOT byte-identical
     Prove that build_dot(src, wiki, policy) with default policy produces output
     BYTE-IDENTICAL to what the pre-Phase-D code (using module-level constants)
     would have produced.  The pre-Phase-D logic is replicated inline so the test
     is self-contained and readable without the original source.

  3. Validator defaults unchanged
     Prove that validate(wiki_dir) with no config= argument uses the same
     NAV_PAGES / REQUIRED_FM / META_TYPES as before the refactor.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Insert repo root so we can import wiki_weaver.* and pipeline.* without installing.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "pipeline"))

from wiki_weaver.policy import WikiPolicy, _DEF_MODEL, _DEF_PROVIDER, load_policy  # noqa: E402
from wiki_weaver.engine_runner import (  # noqa: E402
    CONVERGENCE_RUBRIC_PATH,
    FOOTNOTES_PY,
    INIT_DOT,
    INNER_DOT,
    LLM_NODE_IDS,
    MODEL,
    NORMALIZE_PY,
    PROVIDER,
    REASONING_EFFORT,
    RUBRIC_PATH,
    SCHEMA_PATH,
    VALIDATE_PY,
    build_ask_dot,
    build_ask_dot_from_file,
    build_dot,
    build_init_dot,
    build_init_dot_from_file,
    build_lint_dot,
    build_lint_dot_from_file,
)
from validate_wiki import (  # noqa: E402
    META_TYPES,
    REQUIRED_FM,
    validate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_wiki(tmp_path: Path) -> Path:
    """Create a minimal wiki directory with no policy/ dir and no wiki.config.yaml.

    Uses ``parents=True`` so callers can pass a nested path like
    ``tmp_path / "default"`` without pre-creating the intermediate directory.
    """
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True)
    (wiki / ".ai").mkdir()
    (wiki / "_inbox").mkdir()
    (wiki / "_archive").mkdir()
    (wiki / ".ai" / "feedback").mkdir()
    return wiki


def _make_source(tmp_path: Path) -> Path:
    """Create a minimal markdown source file for DOT substitution tests."""
    src = tmp_path / "source.md"
    src.write_text(
        "---\ntitle: Test Article\nauthor: Alice\nsource: http://example.com/test\n---\n"
        "# Test\n\nThis is test content.\n",
        encoding="utf-8",
    )
    return src


def _build_dot_pre_phase_d(
    source_path: Path,
    wiki_dir: Path,
    max_cycles: int,
    source_id: int | str = "",
) -> str:
    """Exact replica of the pre-Phase-D build_dot logic.

    Uses the same module-level constants (INNER_DOT, SCHEMA_PATH, etc.) and
    applies the same substitutions and per-node injection as the original code.
    This function never changes — it captures what pre-Phase-D produced.
    """
    import sys as _sys

    dot = INNER_DOT.read_text(encoding="utf-8")
    validation_report = wiki_dir / ".ai" / "validation.md"
    validate_cmd = (
        f"{_sys.executable} {VALIDATE_PY} {wiki_dir} --out {validation_report}"
    )
    normalize_cmd = f"{_sys.executable} {NORMALIZE_PY} {wiki_dir}"
    footnotes_cmd = f"{_sys.executable} {FOOTNOTES_PY} {wiki_dir}"
    substitutions = {
        "$source_path": str(source_path),
        "$wiki_dir": str(wiki_dir),
        "$validation_report": str(validation_report),
        "$schema_path": str(SCHEMA_PATH),
        "$convergence_rubric": str(CONVERGENCE_RUBRIC_PATH),
        "$rubric_path": str(RUBRIC_PATH),
        "$normalize_cmd": normalize_cmd,
        "$footnotes_cmd": footnotes_cmd,
        "$validate_cmd": validate_cmd,
        "$max_cycles": str(max_cycles),
        "$source_id": str(source_id),
    }
    for var, value in substitutions.items():
        dot = dot.replace(var, value)

    re_attr = f' reasoning_effort="{REASONING_EFFORT}",' if REASONING_EFFORT else ""
    attrs = f'        llm_provider="{PROVIDER}", llm_model="{MODEL}",{re_attr}\n'
    for nid in LLM_NODE_IDS:
        opener = f"    {nid} [\n"
        if opener in dot and "llm_provider" not in dot.split(opener, 1)[1][:200]:
            dot = dot.replace(opener, opener + attrs, 1)
    return dot


# ---------------------------------------------------------------------------
# Group 1 — load_policy defaults
# ---------------------------------------------------------------------------


class TestLoadPolicyDefaults:
    """load_policy on a wiki with no policy/ dir and no wiki.config.yaml."""

    def test_schema_path_is_builtin(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        policy = load_policy(wiki)
        assert policy.schema_path == SCHEMA_PATH
        assert policy.schema_path.is_file(), "built-in SCHEMA.md must exist"

    def test_convergence_rubric_path_is_builtin(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        policy = load_policy(wiki)
        assert policy.convergence_rubric_path == CONVERGENCE_RUBRIC_PATH
        assert policy.convergence_rubric_path.is_file()

    def test_inner_dot_path_is_builtin(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        policy = load_policy(wiki)
        assert policy.inner_dot_path == INNER_DOT
        assert policy.inner_dot_path.is_file()

    def test_validator_config_path_is_none(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        policy = load_policy(wiki)
        assert policy.validator_config_path is None

    def test_provider_is_default(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        policy = load_policy(wiki)
        assert policy.provider == _DEF_PROVIDER

    def test_model_for_ingest_is_default(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        policy = load_policy(wiki)
        assert policy.model_for("ingest") == _DEF_MODEL

    def test_model_for_assess_is_default(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        policy = load_policy(wiki)
        assert policy.model_for("assess") == _DEF_MODEL

    def test_model_for_feedback_is_default(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        policy = load_policy(wiki)
        assert policy.model_for("feedback") == _DEF_MODEL

    def test_model_for_ask_is_default(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        policy = load_policy(wiki)
        assert policy.model_for("ask") == _DEF_MODEL

    def test_max_cycles_default_is_3(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        policy = load_policy(wiki)
        assert policy.max_cycles == 3

    def test_cli_max_cycles_override(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        policy = load_policy(wiki, cli_max_cycles=5)
        assert policy.max_cycles == 5

    def test_parallelism_default_is_1(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        policy = load_policy(wiki)
        assert policy.parallelism == 1

    def test_frozen_dataclass(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        policy = load_policy(wiki)
        assert isinstance(policy, WikiPolicy)
        with pytest.raises((AttributeError, TypeError)):
            policy.max_cycles = 99  # type: ignore[misc]

    def test_wiki_config_yaml_overrides_max_cycles(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        (wiki / "wiki.config.yaml").write_text("max_cycles: 7\n", encoding="utf-8")
        policy = load_policy(wiki)
        assert policy.max_cycles == 7

    def test_wiki_config_yaml_cli_beats_file(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        (wiki / "wiki.config.yaml").write_text("max_cycles: 7\n", encoding="utf-8")
        policy = load_policy(wiki, cli_max_cycles=2)
        assert policy.max_cycles == 2

    def test_project_schema_overrides_builtin(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        (wiki / "policy").mkdir()
        project_schema = wiki / "policy" / "schema.md"
        project_schema.write_text("# Project Schema\n", encoding="utf-8")
        policy = load_policy(wiki)
        assert policy.schema_path == project_schema
        assert policy.schema_path != SCHEMA_PATH

    def test_project_validator_yaml_sets_config_path(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        (wiki / "policy").mkdir()
        vcfg = wiki / "policy" / "validator.yaml"
        vcfg.write_text("nav_pages: [catalog, landing]\n", encoding="utf-8")
        policy = load_policy(wiki)
        assert policy.validator_config_path == vcfg

    def test_absent_policy_file_does_not_set_config_path(self, tmp_path: Path) -> None:
        """A policy/ dir without validator.yaml keeps validator_config_path=None."""
        wiki = _make_wiki(tmp_path)
        (wiki / "policy").mkdir()
        (wiki / "policy" / "schema.md").write_text("# Schema\n", encoding="utf-8")
        policy = load_policy(wiki)
        # validator.yaml was NOT created
        assert policy.validator_config_path is None

    def test_side_by_side_two_policies(self, tmp_path: Path) -> None:
        """One engine, two policies — the core reusability claim."""
        default_wiki = _make_wiki(tmp_path / "default")
        project_wiki = _make_wiki(tmp_path / "project")

        (project_wiki / "policy").mkdir()
        (project_wiki / "policy" / "schema.md").write_text(
            "# Board Game Schema\n", encoding="utf-8"
        )
        (project_wiki / "wiki.config.yaml").write_text(
            "max_cycles: 2\n", encoding="utf-8"
        )

        default_policy = load_policy(default_wiki)
        project_policy = load_policy(project_wiki)

        # Default wiki uses built-in schema
        assert default_policy.schema_path == SCHEMA_PATH
        assert default_policy.max_cycles == 3
        # Project wiki uses project schema and different max_cycles
        assert project_policy.schema_path == project_wiki / "policy" / "schema.md"
        assert project_policy.max_cycles == 2


# ---------------------------------------------------------------------------
# Group 2 — Golden DOT byte-identical
# ---------------------------------------------------------------------------


class TestBuildDotByteIdentical:
    """With default policy (no project overrides), build_dot output must be
    BYTE-IDENTICAL to the pre-Phase-D code that used module-level constants."""

    def test_default_policy_dot_byte_identical(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        src = _make_source(tmp_path)
        max_cycles = 3
        source_id = 1

        policy = load_policy(wiki)
        new_dot = build_dot(src, wiki, policy, source_id=source_id)
        old_dot = _build_dot_pre_phase_d(src, wiki, max_cycles, source_id=source_id)

        assert new_dot == old_dot, (
            "build_dot with default policy MUST be byte-identical to pre-Phase-D output.\n"
            f"First difference at position: "
            f"{next((i for i, (a, b) in enumerate(zip(new_dot, old_dot)) if a != b), len(old_dot))}"
        )

    def test_default_policy_dot_byte_identical_source_id_zero(
        self, tmp_path: Path
    ) -> None:
        wiki = _make_wiki(tmp_path)
        src = _make_source(tmp_path)
        policy = load_policy(wiki)
        new_dot = build_dot(src, wiki, policy, source_id="")
        old_dot = _build_dot_pre_phase_d(src, wiki, 3, source_id="")
        assert new_dot == old_dot

    def test_default_policy_no_config_in_validate_cmd(self, tmp_path: Path) -> None:
        """Default policy must NOT inject --config into validate_cmd."""
        wiki = _make_wiki(tmp_path)
        src = _make_source(tmp_path)
        policy = load_policy(wiki)
        dot = build_dot(src, wiki, policy)
        assert "--config" not in dot, (
            "Default policy must not inject --config into validate_cmd"
        )

    def test_project_policy_injects_config_in_validate_cmd(
        self, tmp_path: Path
    ) -> None:
        """When validator.yaml exists, --config must appear in validate_cmd."""
        wiki = _make_wiki(tmp_path)
        src = _make_source(tmp_path)
        (wiki / "policy").mkdir()
        vcfg = wiki / "policy" / "validator.yaml"
        vcfg.write_text("nav_pages: [catalog, landing]\n", encoding="utf-8")
        policy = load_policy(wiki)
        dot = build_dot(src, wiki, policy)
        assert f"--config {vcfg}" in dot, (
            "Project policy must inject --config <validator.yaml> into validate_cmd"
        )


# ---------------------------------------------------------------------------
# Group 2b — ask DOT byte-identical
# ---------------------------------------------------------------------------


class TestBuildAskDotByteIdentical:
    """build_ask_dot_from_file must be BYTE-IDENTICAL to build_ask_dot for the
    same inputs.  All tests are deterministic — no LLM, no network, no API key."""

    def test_default_inputs_byte_identical(self, tmp_path: Path) -> None:
        """Simple question, default provider/model."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        af = tmp_path / "ans.json"
        question = "what is a wiki?"
        assert build_ask_dot(wiki, question, af) == build_ask_dot_from_file(
            wiki, question, af
        ), "build_ask_dot_from_file must be byte-identical to build_ask_dot"

    def test_question_with_double_quotes_byte_identical(self, tmp_path: Path) -> None:
        """Question containing double-quotes (DOT-special) must be escaped identically."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        af = tmp_path / "ans.json"
        question = 'what does "idempotent" mean in this context?'
        assert build_ask_dot(wiki, question, af) == build_ask_dot_from_file(
            wiki, question, af
        )

    def test_question_with_newline_byte_identical(self, tmp_path: Path) -> None:
        """Question containing a literal newline (DOT-special) must be escaped identically."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        af = tmp_path / "ans.json"
        question = "first line\nsecond line of the question"
        assert build_ask_dot(wiki, question, af) == build_ask_dot_from_file(
            wiki, question, af
        )

    def test_question_with_backslash_byte_identical(self, tmp_path: Path) -> None:
        """Question containing a backslash (DOT-special) must be escaped identically."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        af = tmp_path / "ans.json"
        question = r"what is path\to\knowledge?"
        assert build_ask_dot(wiki, question, af) == build_ask_dot_from_file(
            wiki, question, af
        )


# ---------------------------------------------------------------------------
# Group 2c — lint DOT byte-identical
# ---------------------------------------------------------------------------


class TestBuildLintDotByteIdentical:
    """build_lint_dot_from_file must be BYTE-IDENTICAL to build_lint_dot for the
    same inputs.  All tests are deterministic — no LLM, no network, no API key,
    no engine run."""

    def test_no_validator_config_byte_identical(self, tmp_path: Path) -> None:
        """Wiki without policy/validator.yaml — no --config flag in the cmd."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        result_file = tmp_path / "lint_result.md"
        assert build_lint_dot(wiki, result_file) == build_lint_dot_from_file(
            wiki, result_file
        ), (
            "build_lint_dot_from_file must be byte-identical to build_lint_dot (no config)"
        )

    def test_with_validator_config_byte_identical(self, tmp_path: Path) -> None:
        """Wiki with policy/validator.yaml present — --config appended to cmd."""
        wiki = tmp_path / "wiki"
        (wiki / "policy").mkdir(parents=True)
        (wiki / "policy" / "validator.yaml").write_text(
            "nav_pages: [index]\n", encoding="utf-8"
        )
        result_file = tmp_path / "lint_result.md"
        assert build_lint_dot(wiki, result_file) == build_lint_dot_from_file(
            wiki, result_file
        ), (
            "build_lint_dot_from_file must be byte-identical to build_lint_dot (with config)"
        )

    def test_config_present_differs_from_absent(self, tmp_path: Path) -> None:
        """Sanity: the two cases (config present / absent) must NOT be identical."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        result_file = tmp_path / "lint_result.md"
        no_cfg = build_lint_dot(wiki, result_file)

        (wiki / "policy").mkdir()
        (wiki / "policy" / "validator.yaml").write_text(
            "nav_pages: [index]\n", encoding="utf-8"
        )
        with_cfg = build_lint_dot(wiki, result_file)

        assert no_cfg != with_cfg, (
            "Adding policy/validator.yaml must change the validate_cmd (--config appended)"
        )

    def test_validate_cmd_contains_wiki_abs_path(self, tmp_path: Path) -> None:
        """The validate_cmd in the DOT must contain the resolved absolute wiki path."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        result_file = tmp_path / "lint_result.md"
        dot = build_lint_dot_from_file(wiki, result_file)
        assert str(wiki.resolve()) in dot

    def test_validate_cmd_contains_result_file(self, tmp_path: Path) -> None:
        """The validate_cmd in the DOT must contain the --out result file path."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        result_file = tmp_path / "lint_result.md"
        dot = build_lint_dot_from_file(wiki, result_file)
        assert str(result_file) in dot
        assert "--out" in dot


# ---------------------------------------------------------------------------
# Group 2d — init DOT byte-identical
# ---------------------------------------------------------------------------


class TestBuildInitDotByteIdentical:
    """build_init_dot_from_file must be BYTE-IDENTICAL to build_init_dot for the
    same inputs.  All tests are deterministic — no LLM, no network, no API key,
    no engine run."""

    def test_simple_purpose_empty_sample_byte_identical(self, tmp_path: Path) -> None:
        """Simple ASCII purpose, empty source_sample."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        purpose = "A coding tools second brain"
        assert build_init_dot(wiki, purpose, "") == build_init_dot_from_file(
            wiki, purpose, ""
        ), "build_init_dot_from_file must be byte-identical to build_init_dot"

    def test_purpose_with_double_quotes_byte_identical(self, tmp_path: Path) -> None:
        """Purpose containing double-quotes (DOT-special) must be escaped identically."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        purpose = 'Research "LLM wiki" patterns for knowledge management'
        assert build_init_dot(wiki, purpose, "") == build_init_dot_from_file(
            wiki, purpose, ""
        )

    def test_purpose_with_newline_byte_identical(self, tmp_path: Path) -> None:
        """Purpose containing a literal newline (DOT-special) must be escaped identically."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        purpose = "AI tooling decisions wiki\noptimised for quick look-up"
        assert build_init_dot(wiki, purpose, "") == build_init_dot_from_file(
            wiki, purpose, ""
        )

    def test_source_sample_with_quotes_and_newlines_byte_identical(
        self, tmp_path: Path
    ) -> None:
        """source_sample containing DOT-special chars must be escaped identically."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        purpose = "team memory"
        source_sample = (
            '---\ntitle: "Meeting Notes"\nauthor: Alice\n---\n# Discussion\nKey point.'
        )
        assert build_init_dot(wiki, purpose, source_sample) == build_init_dot_from_file(
            wiki, purpose, source_sample
        )

    def test_empty_purpose_and_sample_byte_identical(self, tmp_path: Path) -> None:
        """Empty purpose and sample (degenerate case) must still be byte-identical."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        assert build_init_dot(wiki, "", "") == build_init_dot_from_file(wiki, "", "")

    def test_init_dot_file_exists(self) -> None:
        """pipeline/init.dot must exist as a real file."""
        assert INIT_DOT.is_file(), f"pipeline/init.dot must exist at {INIT_DOT}"

    def test_init_dot_contains_tokens(self) -> None:
        """pipeline/init.dot must contain all four substitution tokens."""
        content = INIT_DOT.read_text(encoding="utf-8")
        for token in ("$wiki_dir", "$purpose", "$source_sample", "$default_schema"):
            assert token in content, f"pipeline/init.dot must contain token {token!r}"

    def test_init_dot_contains_design_schema_node(self) -> None:
        """pipeline/init.dot must declare the design_schema LLM node."""
        content = INIT_DOT.read_text(encoding="utf-8")
        assert "design_schema" in content
        assert 'llm_provider="anthropic"' in content
        assert 'llm_model="claude-sonnet-4-6"' in content

    def test_wiki_dir_embedded_in_output(self, tmp_path: Path) -> None:
        """The wiki directory absolute path must appear in both builders' output."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        wiki_abs = str(wiki.resolve())
        dot = build_init_dot_from_file(wiki, "test", "")
        assert wiki_abs in dot, "wiki_dir absolute path must be embedded in DOT output"

    def test_provider_model_override(self, tmp_path: Path) -> None:
        """Supplying provider/model replaces the baked defaults in both builders."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        dot_ref = build_init_dot(wiki, "test", "", provider="openai", model="gpt-4o")
        dot_file = build_init_dot_from_file(
            wiki, "test", "", provider="openai", model="gpt-4o"
        )
        assert dot_ref == dot_file
        assert 'llm_provider="openai"' in dot_ref
        assert 'llm_model="gpt-4o"' in dot_ref


# ---------------------------------------------------------------------------
# Group 3 — Validator defaults unchanged
# ---------------------------------------------------------------------------


class TestValidatorDefaults:
    """validate() with no config= must use the built-in module constants."""

    def _make_valid_wiki(self, wiki: Path) -> None:
        """Write a small structurally-valid wiki (pass case for default validator)."""
        (wiki / "index.md").write_text(
            "---\ntitle: Index\ntype: index\nsources: []\n---\n"
            "# Index\n\nSee [[Alpha]].\n",
            encoding="utf-8",
        )
        (wiki / "alpha.md").write_text(
            "---\ntitle: Alpha\ntype: concept\nsources: [1]\n---\n"
            "# Alpha\n\nContent.\n",
            encoding="utf-8",
        )

    def test_nav_pages_exempt_from_orphan_check(self, tmp_path: Path) -> None:
        """Built-in NAV_PAGES (index, overview, readme, log) must be orphan-exempt."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        self._make_valid_wiki(wiki)
        r = validate(wiki)  # no config
        # Index page should NOT be listed as an orphan (it's in NAV_PAGES)
        orphans = r["checks"]["S2_no_orphans"]["detail"]
        assert "index" not in orphans, f"'index' should not be an orphan: {orphans}"

    def test_required_fm_defaults(self, tmp_path: Path) -> None:
        """Without config, required frontmatter = ('title', 'type', 'sources')."""
        assert REQUIRED_FM == ("title", "type", "sources"), (
            "REQUIRED_FM constant must match spec"
        )

    def test_meta_types_defaults(self, tmp_path: Path) -> None:
        """Without config, meta types = {'index', 'overview', 'log', 'meta'}."""
        assert META_TYPES == {"index", "overview", "log", "meta"}, (
            "META_TYPES constant must match spec"
        )

    def test_validate_no_config_equals_validate_with_defaults(
        self, tmp_path: Path
    ) -> None:
        """validate(wiki) == validate(wiki, config={}) on a real fixture."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        self._make_valid_wiki(wiki)
        r_no_config = validate(wiki)
        r_empty_config = validate(wiki, config={})
        # Both should report the same pass/fail and check structure
        assert r_no_config["passed"] == r_empty_config["passed"]
        for key in r_no_config["checks"]:
            assert r_no_config["checks"][key] == r_empty_config["checks"][key]

    def test_config_nav_pages_overrides_default(self, tmp_path: Path) -> None:
        """Providing nav_pages=['catalog', 'landing'] overrides the built-in set."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        # catalog.md is a nav page under the custom config
        (wiki / "catalog.md").write_text(
            "---\ntitle: Catalog\ntype: catalog\nsources: []\n---\n# Catalog\n",
            encoding="utf-8",
        )

        # Default validator: 'catalog' is NOT in NAV_PAGES → flagged as orphan
        r_default = validate(wiki)
        orphans_default = r_default["checks"]["S2_no_orphans"]["detail"]
        assert "catalog" in orphans_default, (
            "Default validator must flag 'catalog' as orphan"
        )

        # Custom validator: 'catalog' IS in nav_pages → not flagged
        r_custom = validate(
            wiki,
            config={"nav_pages": ["catalog", "landing"], "meta_types": ["catalog"]},
        )
        orphans_custom = r_custom["checks"]["S2_no_orphans"]["detail"]
        assert "catalog" not in orphans_custom, (
            "Custom validator must NOT flag 'catalog' as orphan when in nav_pages"
        )
