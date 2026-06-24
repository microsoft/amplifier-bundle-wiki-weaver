"""Runtime model resolver for wiki-weaver.

Resolves family tokens ("opus", "sonnet", "haiku") to the newest stable served
model id in that family by delegating to ``unified_llm.resolve_latest_for``.
Explicit model ids (e.g. "claude-sonnet-4-6") are returned unchanged — no
network call.

Design goals
------------
- Zero-maintenance: users specify a family name and always get the newest stable
  model the provider *actually serves* — no version-pin to keep up to date.
- Fail loud: network failure, missing API key, or a family that matches zero
  served stable models raises a clear, actionable error.  No silent fallback.
- Process-level cache: resolution per (provider, family) happens at most once
  per process run, so a long ingest loop pays one round-trip per family.

Supported providers for family-token resolution
------------------------------------------------
Only "anthropic" supports family tokens today.  For other providers the caller
must pass an explicit model id; a family token raises ValueError.
"""

from __future__ import annotations

import asyncio

from unified_llm import resolve_latest_for  # noqa: F401 (patched by tests)

# Known family tokens (case-insensitive full-token match against spec).
KNOWN_FAMILIES: frozenset[str] = frozenset({"opus", "sonnet", "haiku"})

# Glob patterns passed to the upstream resolver per family.
_FAMILY_GLOB: dict[str, str] = {
    "opus": "*opus*",
    "sonnet": "*sonnet*",
    "haiku": "*haiku*",
}

# Process-level cache: (provider, family_token) -> concrete_model_id
# Populated lazily; cleared by tests via _clear_cache().
_CACHE: dict[tuple[str, str], str] = {}


def _clear_cache() -> None:
    """Clear the process-level resolution cache.  Used by tests."""
    _CACHE.clear()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_model(provider: str, spec: str) -> str:
    """Resolve *spec* to a concrete served model id for *provider*.

    Parameters
    ----------
    provider:
        Provider name (e.g. ``"anthropic"``).
    spec:
        Either a family token (``"opus"``, ``"sonnet"``, ``"haiku"``) or an
        explicit model id (``"claude-sonnet-4-6"``).

    Returns
    -------
    str
        Concrete model id that the provider currently serves.

    Raises
    ------
    ValueError
        * Family token used with a provider that doesn't support them.
        * Family token matches zero stable models in the live list.
    RuntimeError
        * Live model list can't be fetched (network / auth failure).
    """
    family = spec.strip().lower()

    # Explicit id — no network call needed.
    if family not in KNOWN_FAMILIES:
        return spec

    # Only anthropic supports family tokens today.
    if provider != "anthropic":
        raise ValueError(
            f"Family tokens ({spec!r}) are only supported for provider='anthropic' today. "
            f"Pass an explicit model id for provider={provider!r}."
        )

    cache_key = (provider, family)
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    # Delegate to the upstream resolver (async, bridged with asyncio.run).
    # The same adapter that lists is the adapter that generates — the resolved
    # id is generation-compatible by construction (id-seam closed upstream).
    glob = _FAMILY_GLOB[family]
    best = asyncio.run(resolve_latest_for(provider, glob, stable_only=True))

    _CACHE[cache_key] = best
    return best
