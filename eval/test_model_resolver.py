"""Unit tests for wiki_weaver.model_resolver.

All tests are keyless and deterministic — they monkeypatch
``wiki_weaver.model_resolver.resolve_latest_for`` (the upstream async function
imported into the module's namespace) so no real network calls are made.

Tests verify the resolver's:

  - family-token resolution (shim routes to upstream, returns its answer)
  - explicit-id pass-through (no upstream call)
  - fail-loud propagation (upstream errors propagate unchanged)
  - fail-loud for non-anthropic provider with a family token (local guard)
  - process-level cache (second call skips upstream entirely)
  - case-insensitive family token matching
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make wiki_weaver importable without installing.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from wiki_weaver.model_resolver import (  # noqa: E402
    _clear_cache,
    resolve_model,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_resolver_cache():
    """Clear the process-level cache before and after each test for isolation."""
    _clear_cache()
    yield
    _clear_cache()


# ---------------------------------------------------------------------------
# resolve_model — explicit id pass-through
# ---------------------------------------------------------------------------


class TestExplicitIdPassthrough:
    """Explicit model ids are returned unchanged — no upstream call."""

    def test_explicit_id_returned_unchanged(self, monkeypatch):
        """Explicit id bypasses resolve_latest_for entirely."""

        async def _must_not_be_called(*a, **kw):
            raise AssertionError(
                "resolve_latest_for must not be called for explicit ids"
            )

        monkeypatch.setattr(
            "wiki_weaver.model_resolver.resolve_latest_for",
            _must_not_be_called,
        )
        assert resolve_model("anthropic", "claude-sonnet-4-6") == "claude-sonnet-4-6"

    def test_explicit_id_with_non_anthropic_provider(self, monkeypatch):
        """Explicit id for any provider bypasses upstream — no guard needed."""

        async def _must_not_be_called(*a, **kw):
            raise AssertionError(
                "resolve_latest_for must not be called for explicit ids"
            )

        monkeypatch.setattr(
            "wiki_weaver.model_resolver.resolve_latest_for",
            _must_not_be_called,
        )
        assert resolve_model("openai", "gpt-4o") == "gpt-4o"

    def test_hyphenated_id_not_a_family(self, monkeypatch):
        """'claude-opus-4-8' contains 'opus' as a substring but is NOT a bare
        family token — it must pass through unchanged."""

        async def _must_not_be_called(*a, **kw):
            raise AssertionError(
                "resolve_latest_for must not be called for explicit ids"
            )

        monkeypatch.setattr(
            "wiki_weaver.model_resolver.resolve_latest_for",
            _must_not_be_called,
        )
        assert resolve_model("anthropic", "claude-opus-4-8") == "claude-opus-4-8"


# ---------------------------------------------------------------------------
# resolve_model — family token resolution
# ---------------------------------------------------------------------------


class TestFamilyTokenResolution:
    """Family tokens delegate to resolve_latest_for and return its answer."""

    def test_sonnet_returns_upstreams_answer(self, monkeypatch):
        """'sonnet' → whatever resolve_latest_for returns for '*sonnet*'."""

        async def _stub(provider, pattern, *, stable_only=True):
            assert provider == "anthropic"
            assert pattern == "*sonnet*"
            assert stable_only is True
            return "claude-sonnet-4-6"

        monkeypatch.setattr("wiki_weaver.model_resolver.resolve_latest_for", _stub)
        result = resolve_model("anthropic", "sonnet")
        assert result == "claude-sonnet-4-6"

    def test_opus_returns_upstreams_answer(self, monkeypatch):
        """'opus' → whatever resolve_latest_for returns for '*opus*'."""

        async def _stub(provider, pattern, *, stable_only=True):
            assert pattern == "*opus*"
            return "claude-opus-4-8"

        monkeypatch.setattr("wiki_weaver.model_resolver.resolve_latest_for", _stub)
        assert resolve_model("anthropic", "opus") == "claude-opus-4-8"

    def test_haiku_returns_upstreams_answer(self, monkeypatch):
        """'haiku' → whatever resolve_latest_for returns for '*haiku*'."""

        async def _stub(provider, pattern, *, stable_only=True):
            assert pattern == "*haiku*"
            return "claude-haiku-4-5"

        monkeypatch.setattr("wiki_weaver.model_resolver.resolve_latest_for", _stub)
        assert resolve_model("anthropic", "haiku") == "claude-haiku-4-5"

    def test_family_token_case_insensitive(self, monkeypatch):
        """'SONNET' (uppercase) should resolve the same as 'sonnet'."""

        async def _stub(provider, pattern, *, stable_only=True):
            assert pattern == "*sonnet*"  # normalised to lowercase glob
            return "claude-sonnet-4-6"

        monkeypatch.setattr("wiki_weaver.model_resolver.resolve_latest_for", _stub)
        assert resolve_model("anthropic", "SONNET") == "claude-sonnet-4-6"

    def test_result_is_cached(self, monkeypatch):
        """Second call for the same (provider, family) must not call upstream again."""
        call_count = [0]

        async def _counting_stub(provider, pattern, *, stable_only=True):
            call_count[0] += 1
            return "claude-sonnet-4-6"

        monkeypatch.setattr(
            "wiki_weaver.model_resolver.resolve_latest_for", _counting_stub
        )

        resolve_model("anthropic", "sonnet")
        resolve_model("anthropic", "sonnet")

        assert call_count[0] == 1, (
            f"Expected 1 upstream call but got {call_count[0]}. Cache is not working."
        )

    def test_different_families_each_call_upstream_once(self, monkeypatch):
        """Two different family tokens each get exactly one upstream call."""
        calls: list[str] = []

        async def _tracking_stub(provider, pattern, *, stable_only=True):
            calls.append(pattern)
            return f"resolved-{pattern.strip('*')}-model"

        monkeypatch.setattr(
            "wiki_weaver.model_resolver.resolve_latest_for", _tracking_stub
        )

        resolve_model("anthropic", "sonnet")
        resolve_model("anthropic", "opus")
        resolve_model("anthropic", "sonnet")  # cached — no call
        resolve_model("anthropic", "opus")  # cached — no call

        assert calls == ["*sonnet*", "*opus*"], (
            f"Expected exactly ['*sonnet*', '*opus*'] but got {calls}"
        )


# ---------------------------------------------------------------------------
# resolve_model — fail-loud cases
# ---------------------------------------------------------------------------


class TestFailLoud:
    """Resolver raises clear errors rather than silently falling back."""

    def test_empty_family_raises_value_error_from_upstream(self, monkeypatch):
        """When upstream raises ValueError (no match), the shim propagates it."""

        async def _no_match(provider, pattern, *, stable_only=True):
            raise ValueError(f"No stable model matching {pattern!r} for {provider!r}")

        monkeypatch.setattr("wiki_weaver.model_resolver.resolve_latest_for", _no_match)
        with pytest.raises(ValueError, match="haiku"):
            resolve_model("anthropic", "haiku")

    def test_non_anthropic_provider_with_family_raises(self):
        """Family tokens for non-anthropic providers raise ValueError immediately
        (local guard — no upstream call needed)."""
        with pytest.raises(ValueError, match="anthropic"):
            resolve_model("openai", "sonnet")

    def test_network_failure_raises_runtime_error_from_upstream(self, monkeypatch):
        """When upstream raises RuntimeError (e.g. auth or network failure),
        the shim propagates it unchanged."""

        async def _auth_failure(provider, pattern, *, stable_only=True):
            raise RuntimeError(f"Failed to list models for {provider!r}: auth error")

        monkeypatch.setattr(
            "wiki_weaver.model_resolver.resolve_latest_for", _auth_failure
        )
        with pytest.raises(RuntimeError):
            resolve_model("anthropic", "sonnet")

    def test_upstream_error_propagates_type_unchanged(self, monkeypatch):
        """Whatever exception type upstream raises must propagate unchanged
        (no swallowing, no wrapping into a different type)."""

        class _CustomUpstreamError(Exception):
            pass

        async def _custom_raise(provider, pattern, *, stable_only=True):
            raise _CustomUpstreamError("something unusual from upstream")

        monkeypatch.setattr(
            "wiki_weaver.model_resolver.resolve_latest_for", _custom_raise
        )
        with pytest.raises(_CustomUpstreamError):
            resolve_model("anthropic", "opus")


# ---------------------------------------------------------------------------
# resolve_model — glob mapping
# ---------------------------------------------------------------------------


class TestGlobMapping:
    """The shim maps each family token to the correct glob before calling upstream."""

    @pytest.mark.parametrize(
        "family,expected_glob",
        [
            ("opus", "*opus*"),
            ("sonnet", "*sonnet*"),
            ("haiku", "*haiku*"),
            ("OPUS", "*opus*"),  # case-normalised before glob lookup
            ("Haiku", "*haiku*"),
        ],
    )
    def test_correct_glob_passed_to_upstream(self, monkeypatch, family, expected_glob):
        """Each family token maps to the right '*token*' glob."""
        received: list[str] = []

        async def _capture(provider, pattern, *, stable_only=True):
            received.append(pattern)
            return "some-model-id"

        monkeypatch.setattr("wiki_weaver.model_resolver.resolve_latest_for", _capture)
        resolve_model("anthropic", family)
        assert received == [expected_glob], (
            f"Family {family!r} should send glob {expected_glob!r}, got {received}"
        )

    @pytest.mark.parametrize("family", ["opus", "sonnet", "haiku"])
    def test_stable_only_always_true(self, monkeypatch, family):
        """The shim always passes stable_only=True to the upstream resolver."""
        received: list[bool] = []

        async def _capture(provider, pattern, *, stable_only=True):
            received.append(stable_only)
            return "some-model-id"

        monkeypatch.setattr("wiki_weaver.model_resolver.resolve_latest_for", _capture)
        resolve_model("anthropic", family)
        assert received == [True], f"stable_only must be True, got {received}"
