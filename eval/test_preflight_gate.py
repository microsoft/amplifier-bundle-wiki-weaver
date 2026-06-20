"""Preflight gate tests.

Cover the shared HARD-prerequisite helper (`preflight`) and the command-wrapper
gate so a broken environment fails CLEAN + UPFRONT (no mid-ingest traceback)
instead of crashing minutes into an engine/LLM run.

SAFETY: pure in-process checks; no engine, no LLM, no real ingest.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from wiki_weaver.lib import preflight  # noqa: E402
from wiki_weaver.wiki_weaver import cmd_ingest, cmd_query  # noqa: E402

# The "env is clean -> preflight returns []" assertion only holds in a fully
# provisioned dev environment. CI is deliberately lightweight (no Amplifier runtime,
# no API key), so that case is skipped there; the real install + @main dependency
# resolution is proven by the DTU install matrix instead.
_FULLY_PROVISIONED = (
    importlib.util.find_spec("amplifier_foundation") is not None
    and importlib.util.find_spec("unified_llm") is not None
    and bool(os.environ.get("ANTHROPIC_API_KEY"))
)


def test_preflight_flags_missing_key_only_when_required(monkeypatch) -> None:
    """The API-key check is gated on require_api_key.

    With the key unset: ingest-style preflight (require_api_key=True) reports the
    key failure; lint-style preflight (require_api_key=False) does not. The
    import/validator checks pass in this env, so the key is the only difference.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with_key = preflight(require_api_key=True)
    no_key = preflight(require_api_key=False)

    assert any("ANTHROPIC_API_KEY" in m for m in with_key), (
        f"engine commands must flag the missing key; got {with_key}"
    )
    assert not any("ANTHROPIC_API_KEY" in m for m in no_key), (
        f"lint must NOT impose a key requirement; got {no_key}"
    )


@pytest.mark.skipif(
    not _FULLY_PROVISIONED,
    reason="needs Amplifier runtime (foundation+unified_llm) + API key; "
    "validated by the DTU install proof, not lightweight CI",
)
def test_preflight_clean_when_env_ok() -> None:
    """In this env (key + foundation + unified_llm + validator present) both
    modes return no failures."""
    assert preflight(require_api_key=False) == []
    assert preflight(require_api_key=True) == []


def test_cmd_ingest_gates_before_engine(monkeypatch, tmp_path, capsys) -> None:
    """cmd_ingest fails UPFRONT (nonzero) with no key, WITHOUT importing the
    engine -- proven by patching ingest() to explode if ever reached."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def _boom(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("ingest() reached despite failed preflight")

    monkeypatch.setattr("wiki_weaver.wiki_weaver.ingest", _boom)

    args = argparse.Namespace(
        wiki=str(tmp_path), source=None, max_cycles=None, keep_going=False
    )
    rc = cmd_ingest(args)

    assert rc != 0, "ingest must exit nonzero when the key is missing"
    out = capsys.readouterr().out
    assert "ANTHROPIC_API_KEY is not set" in out
    assert "wiki-weaver doctor" in out


def test_cmd_query_not_gated(monkeypatch, tmp_path) -> None:
    """query is a pure substring grep -- it must work offline with no key."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / "page.md").write_text("alpha gamma\n", encoding="utf-8")

    args = argparse.Namespace(wiki=str(tmp_path), term="gamma")
    assert cmd_query(args) == 0
