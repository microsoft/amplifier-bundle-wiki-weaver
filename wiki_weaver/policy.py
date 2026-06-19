"""Project-policy loader for wiki-weaver (Phase D — schema externalization).

Each wiki MAY carry project-supplied policy files under <wiki>/policy/ and a
knobs file at <wiki>/wiki.config.yaml.  Everything is optional: a wiki with no
policy/ dir and no wiki.config.yaml behaves byte-identically to pre-Phase-D.

Policy files (all optional, each falls back to the built-in pipeline/ default):
  <wiki>/policy/schema.md           overrides pipeline/SCHEMA.md
  <wiki>/policy/convergence-rubric.md  overrides pipeline/CONVERGENCE_RUBRIC.md
  <wiki>/policy/validator.yaml      overrides validate_wiki.py constants
  <wiki>/policy/inner.dot           ADVANCED: override the whole inner pipeline DOT

Knobs file (<wiki>/wiki.config.yaml, all keys optional):
  provider: anthropic
  models:
    default: claude-sonnet-4-6
    ingest:   claude-sonnet-4-6   # per-stage override
    assess:   claude-sonnet-4-6
    feedback: claude-haiku-4-5
  max_cycles: 3
  parallelism: 1                  # RESERVED — honored as 1 (see §5 of the spec)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------
# Built-in defaults (the engine's own pipeline/ assets).
# --------------------------------------------------------------------------

_PIPELINE = Path(__file__).resolve().parent.parent / "pipeline"
_DEF_SCHEMA = _PIPELINE / "SCHEMA.md"
_DEF_RUBRIC = _PIPELINE / "CONVERGENCE_RUBRIC.md"
_DEF_INNERDOT = _PIPELINE / "synthesize.dot"
_DEF_MODEL = os.environ.get("WIKI_WEAVER_MODEL", "claude-sonnet-4-6")
_DEF_PROVIDER = os.environ.get("WIKI_WEAVER_PROVIDER", "anthropic")


# --------------------------------------------------------------------------
# WikiPolicy
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WikiPolicy:
    """Resolved policy for one wiki directory.

    All path fields point at EXISTING files (either project-supplied or the
    built-in pipeline/ defaults).  ``validator_config_path`` is None when no
    project validator.yaml exists — the validator then uses its own built-in
    defaults unchanged.
    """

    schema_path: Path
    convergence_rubric_path: Path
    inner_dot_path: Path
    validator_config_path: Path | None  # None => validator uses built-in defaults
    provider: str
    models: dict[str, str]  # stage -> model; "default" is the fallback
    max_cycles: int
    parallelism: int  # accepted in schema, RESERVED as 1 (sequential correctness)

    def model_for(self, stage: str) -> str:
        """Return the resolved model for *stage*, falling back to default."""
        return self.models.get(stage) or self.models.get("default") or _DEF_MODEL


# --------------------------------------------------------------------------
# load_policy
# --------------------------------------------------------------------------


def load_policy(wiki_dir: Path, *, cli_max_cycles: int | None = None) -> WikiPolicy:
    """Load the project policy for *wiki_dir*.

    Reads <wiki_dir>/wiki.config.yaml and <wiki_dir>/policy/*.  Every element
    is optional: a wiki with neither file behaves byte-identically to pre-Phase-D
    because the built-in defaults are the same constants the engine previously used.

    ``cli_max_cycles`` — when set, overrides wiki.config.yaml's max_cycles (CLI
    flag beats config file).
    """
    wiki_dir = Path(wiki_dir).expanduser().resolve()
    pol = wiki_dir / "policy"

    def _pick(name: str, default: Path) -> Path:
        """Return the project override if it exists, else the built-in default."""
        cand = pol / name
        return cand if cand.is_file() else default

    cfg = _read_yaml(wiki_dir / "wiki.config.yaml")  # {} if absent/unreadable

    models: dict[str, str] = dict(cfg.get("models") or {})
    models.setdefault("default", _DEF_MODEL)

    validator_cfg = pol / "validator.yaml"

    return WikiPolicy(
        schema_path=_pick("schema.md", _DEF_SCHEMA),
        convergence_rubric_path=_pick("convergence-rubric.md", _DEF_RUBRIC),
        inner_dot_path=_pick("inner.dot", _DEF_INNERDOT),
        validator_config_path=validator_cfg if validator_cfg.is_file() else None,
        provider=str(cfg.get("provider") or _DEF_PROVIDER),
        models=models,
        max_cycles=int(
            cli_max_cycles if cli_max_cycles is not None else cfg.get("max_cycles", 3)
        ),
        parallelism=int(cfg.get("parallelism", 1)),
    )


# --------------------------------------------------------------------------
# _read_yaml  (tolerant reader — mirrors load_ci_config pattern)
# --------------------------------------------------------------------------


def _read_yaml(path: Path) -> dict:
    """Read a YAML file and return its contents as a dict.

    Returns {} on any failure (missing file, missing yaml module, parse error).
    Mirrors the tolerant pattern used in engine_runner.load_ci_config.
    """
    try:
        import yaml  # pyright: ignore[reportMissingModuleSource]
    except Exception:  # noqa: BLE001
        return {}
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}
