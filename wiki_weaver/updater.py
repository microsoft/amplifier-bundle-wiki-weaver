# pyright: reportMissingImports=false
"""wiki-weaver source freshness utilities.

Strategy: track @main, fix-forward — no SHA pinning.

Two-layer refresh:
  Layer 1 — the wiki-weaver uv-tool env itself (wiki_weaver + its wheel deps:
             amplifier-foundation and amplifier-unified-llm-client). Triggered
             by ``uv tool install --reinstall`` with a verify+ladder+fail-loud
             loop so stale uv caches are detected and escalated.
  Layer 2 — engine bundles managed by foundation's GitSourceHandler in
             ~/.amplifier/cache/bundles (attractor bundle, CI hook). Refreshed
             via foundation's public GitSourceHandler.update() (rmtree+reclone).

resolve() is NEVER touched (adding ls-remote there would hit the network on
every startup, break offline use, and create intra-run version drift — a
confirmed non-starter per council review).
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Source registry
# ---------------------------------------------------------------------------

# The uv-tool install URL for wiki-weaver itself.
INSTALL_URL = "git+https://github.com/microsoft/amplifier-bundle-wiki-weaver"

# Layer-2 @main git sources managed by foundation's GitSourceHandler.
# The cache key is per-repo (ignores #subdirectory), so root-repo URIs are used.
_LAYER2_SOURCES: list[tuple[str, str]] = [
    (
        "attractor-bundle",
        "git+https://github.com/microsoft/amplifier-bundle-attractor@main",
    ),
    (
        "context-intelligence",
        "git+https://github.com/microsoft/amplifier-bundle-context-intelligence@main",
    ),
]

# Layer-1 wheel deps resolved from git by uv on install/reinstall.
# (dist-package-name, plain-https-url-for-ls-remote)
_LAYER1_WHEEL_DEPS: list[tuple[str, str]] = [
    (
        "amplifier-foundation",
        "https://github.com/microsoft/amplifier-foundation",
    ),
    (
        "amplifier-unified-llm-client",
        "https://github.com/microsoft/amplifier-bundle-attractor",
    ),
]


# ---------------------------------------------------------------------------
# Foundation lazy helpers
# ---------------------------------------------------------------------------


def _cache_dir() -> Path:
    from amplifier_foundation.paths.resolution import get_amplifier_home  # type: ignore[import]

    return get_amplifier_home() / "cache" / "bundles"


def _git_handler():
    from amplifier_foundation.sources.git import GitSourceHandler  # type: ignore[import]

    return GitSourceHandler()


def _parse(uri: str):
    from amplifier_foundation.paths.resolution import parse_uri  # type: ignore[import]

    return parse_uri(uri)


# ---------------------------------------------------------------------------
# SourceRecord: unified result type
# ---------------------------------------------------------------------------


@dataclass
class SourceRecord:
    """Result of a check-or-update operation on one @main git source."""

    label: str
    uri: str
    local_sha: Optional[str] = None
    """Current locally-resolved commit (before update or from cache)."""
    target_sha: Optional[str] = None
    """Target commit: remote HEAD (check) or new-local (after update)."""
    needs_update: Optional[bool] = None
    """True when update is warranted; None when unknown (e.g. network error)."""
    skipped: bool = False
    error: Optional[str] = None

    @property
    def local_short(self) -> str:
        return (self.local_sha or "")[:8] or "(not cached)"

    @property
    def target_short(self) -> str:
        return (self.target_sha or "")[:8] or "(unknown)"

    @property
    def is_mutable(self) -> bool:
        """True when ref is a branch name (@main/HEAD), not a pinned SHA/tag."""
        after_scheme = self.uri.split("://", 1)[-1]
        ref = (
            after_scheme.rsplit("@", 1)[-1].split("#")[0] if "@" in after_scheme else ""
        )
        if len(ref) == 40 and all(c in "0123456789abcdef" for c in ref.lower()):
            return False  # full SHA
        if ref.startswith("v") and any(c.isdigit() for c in ref):
            return False  # version tag
        return True  # branch name = mutable


# ---------------------------------------------------------------------------
# Layer-2: check (ls-remote, no side effects)
# ---------------------------------------------------------------------------


async def _check_l2(label: str, uri: str) -> SourceRecord:
    rec = SourceRecord(label=label, uri=uri)
    try:
        handler = _git_handler()
        parsed = _parse(uri)
        status = await handler.get_status(parsed, _cache_dir())
        rec.local_sha = status.cached_commit
        rec.target_sha = status.remote_commit
        rec.needs_update = status.has_update
        if status.error:
            rec.error = status.error
    except Exception as e:  # noqa: BLE001
        rec.error = str(e)
    return rec


async def _check_l2_all() -> list[SourceRecord]:
    return list(
        await asyncio.gather(*[_check_l2(lbl, uri) for lbl, uri in _LAYER2_SOURCES])
    )


def check_layer2() -> list[SourceRecord]:
    """ls-remote all Layer-2 @main sources and return status (no side effects)."""
    return asyncio.run(_check_l2_all())


# ---------------------------------------------------------------------------
# Layer-2: update (rmtree+reclone via foundation's public update())
# ---------------------------------------------------------------------------


async def _update_l2(label: str, uri: str) -> SourceRecord:
    rec = SourceRecord(label=label, uri=uri)
    if not rec.is_mutable:
        rec.skipped = True
        rec.error = "skipped: ref is pinned"
        return rec
    try:
        handler = _git_handler()
        parsed = _parse(uri)
        cache_dir = _cache_dir()
        cache_path = handler._get_cache_path(parsed, cache_dir)
        rec.local_sha = handler._get_local_commit(cache_path)
        await handler.update(parsed, cache_dir)
        rec.target_sha = handler._get_local_commit(cache_path)
        rec.needs_update = rec.local_sha != rec.target_sha
    except Exception as e:  # noqa: BLE001
        rec.error = str(e)
    return rec


async def _update_l2_all() -> list[SourceRecord]:
    return list(
        await asyncio.gather(*[_update_l2(lbl, uri) for lbl, uri in _LAYER2_SOURCES])
    )


def update_layer2() -> list[SourceRecord]:
    """Re-clone all mutable Layer-2 @main sources.  Returns before→after info."""
    return asyncio.run(_update_l2_all())


# ---------------------------------------------------------------------------
# Layer-2: local-only commit read (for doctor — no network)
# ---------------------------------------------------------------------------


def local_layer2_commits() -> list[SourceRecord]:
    """Read locally-cached commits for Layer-2 sources — no ls-remote."""
    results: list[SourceRecord] = []
    try:
        handler = _git_handler()
        cache_dir = _cache_dir()
        for label, uri in _LAYER2_SOURCES:
            rec = SourceRecord(label=label, uri=uri)
            try:
                parsed = _parse(uri)
                cache_path = handler._get_cache_path(parsed, cache_dir)
                rec.local_sha = handler._get_local_commit(cache_path)
            except Exception as e:  # noqa: BLE001
                rec.error = str(e)
            results.append(rec)
    except Exception as e:  # noqa: BLE001
        for label, uri in _LAYER2_SOURCES:
            results.append(SourceRecord(label=label, uri=uri, error=str(e)))
    return results


# ---------------------------------------------------------------------------
# Layer-1 wheel dep: commit reading via PEP 610 direct_url.json
# ---------------------------------------------------------------------------


def _installed_commit(package_name: str) -> Optional[str]:
    """Read installed git commit SHA from PEP 610 direct_url.json.

    Reads from disk (not an in-memory cache), so it reflects the filesystem
    state even within the same process after a ``uv tool install --reinstall``.
    ``importlib.invalidate_caches()`` flushes any stale path lookups first.
    """
    try:
        importlib.invalidate_caches()
        dist = importlib.metadata.distribution(package_name)
        raw = dist.read_text("direct_url.json")
        if raw:
            info = json.loads(raw)
            return info.get("vcs_info", {}).get("commit_id")
    except Exception:  # noqa: BLE001
        pass
    return None


def wheel_dep_commits() -> list[SourceRecord]:
    """Read installed commits for Layer-1 wheel deps — no network."""
    results: list[SourceRecord] = []
    for name, git_url in _LAYER1_WHEEL_DEPS:
        rec = SourceRecord(label=name, uri=f"git+{git_url}@main")
        rec.local_sha = _installed_commit(name)
        results.append(rec)
    return results


# ---------------------------------------------------------------------------
# Layer-1: check (installed vs remote via ls-remote)
# ---------------------------------------------------------------------------


async def _get_remote_commit_for(git_url: str) -> Optional[str]:
    try:
        return await _git_handler()._get_remote_commit(git_url, "main")
    except Exception:  # noqa: BLE001
        return None


async def _check_wheel_dep(name: str, git_url: str) -> SourceRecord:
    rec = SourceRecord(label=name, uri=f"git+{git_url}@main")
    rec.local_sha = _installed_commit(name)
    rec.target_sha = await _get_remote_commit_for(git_url)
    if rec.local_sha and rec.target_sha:
        rec.needs_update = rec.local_sha != rec.target_sha
    return rec


async def _check_l1_all() -> list[SourceRecord]:
    return list(
        await asyncio.gather(*[_check_wheel_dep(n, u) for n, u in _LAYER1_WHEEL_DEPS])
    )


def check_layer1() -> list[SourceRecord]:
    """ls-remote all Layer-1 wheel deps and compare to installed commits."""
    return asyncio.run(_check_l1_all())


# ---------------------------------------------------------------------------
# Layer-1 update: uv tool install --reinstall + verify + ladder + fail-loud
# ---------------------------------------------------------------------------


@dataclass
class Layer1Result:
    """Outcome of the Layer-1 uv reinstall ladder."""

    success: bool = False
    rung_reached: int = 0
    """1 = plain reinstall, 2 = --no-cache, 3 = cache-clean+reinstall."""
    before: dict[str, Optional[str]] = field(default_factory=dict)
    after: dict[str, Optional[str]] = field(default_factory=dict)
    remote: dict[str, Optional[str]] = field(default_factory=dict)
    stale: list[str] = field(default_factory=list)
    """Packages whose remote had moved but installed commit didn't update."""
    errors: list[str] = field(default_factory=list)


def _run_install(*, no_cache: bool = False) -> tuple[int, str]:
    """Run uv tool install --reinstall [--no-cache].  Returns (rc, stderr)."""
    cmd = ["uv", "tool", "install", "--reinstall"]
    if no_cache:
        cmd.append("--no-cache")
    cmd.append(INSTALL_URL)
    r = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
    return r.returncode, r.stderr


def _run_cache_clean(names: list[str]) -> int:
    cmd = ["uv", "cache", "clean", *names]
    return subprocess.run(cmd, capture_output=True, text=True).returncode  # noqa: S603


async def _fetch_remotes() -> list[Optional[str]]:
    return list(
        await asyncio.gather(
            *[_get_remote_commit_for(u) for _, u in _LAYER1_WHEEL_DEPS]
        )
    )


def update_layer1(*, verbose: bool = False) -> Layer1Result:
    """Run the Layer-1 uv reinstall ladder with verify+fail-loud.

    Ladder:
      Rung 1: ``uv tool install --reinstall <url>``
      Rung 2: ``uv tool install --reinstall --no-cache <url>``
      Rung 3: ``uv cache clean <deps>`` then ``uv tool install --reinstall <url>``

    After each rung, verifies that packages whose remote HEAD had moved are
    now installed at the new remote commit.  A package is only flagged stale
    when remote != before AND installed-after != remote (i.e., change expected
    but didn't happen).  If stale after all rungs, ``result.success`` is False.
    """
    res = Layer1Result()

    # Pre-step: capture remote HEAD + currently-installed commits
    try:
        remotes: list[Optional[str]] = asyncio.run(_fetch_remotes())
    except Exception as e:  # noqa: BLE001
        res.errors.append(f"pre-check ls-remote failed: {e}")
        remotes = [None] * len(_LAYER1_WHEEL_DEPS)

    for (name, _), remote in zip(_LAYER1_WHEEL_DEPS, remotes):
        res.before[name] = _installed_commit(name)
        res.remote[name] = remote

    if verbose:
        for name, _ in _LAYER1_WHEEL_DEPS:
            b = (res.before[name] or "?")[:8]
            r = (res.remote[name] or "?")[:8]
            print(f"  {name}: installed={b}  remote={r}", flush=True)

    def _check_stale() -> list[str]:
        stale: list[str] = []
        for name, _ in _LAYER1_WHEEL_DEPS:
            after = _installed_commit(name)
            res.after[name] = after
            before = res.before.get(name)
            remote = res.remote.get(name)
            # Flag stale only when remote had moved but installed didn't follow
            if remote and before and remote != before and after != remote:
                stale.append(name)
        return stale

    # Rung 1: plain --reinstall
    rc, err = _run_install()
    res.rung_reached = 1
    if rc != 0:
        res.errors.append(f"rung-1 failed (exit {rc}): {err[:300]}")
        return res
    stale = _check_stale()
    if not stale:
        res.success = True
        return res

    if verbose:
        print(f"  ! rung-1: still stale: {stale}. Trying --no-cache…", file=sys.stderr)

    # Rung 2: --reinstall --no-cache
    rc, err = _run_install(no_cache=True)
    res.rung_reached = 2
    if rc != 0:
        res.errors.append(f"rung-2 failed (exit {rc}): {err[:300]}")
        return res
    stale = _check_stale()
    if not stale:
        res.success = True
        return res

    if verbose:
        print(
            f"  ! rung-2: still stale: {stale}. Trying cache clean + reinstall…",
            file=sys.stderr,
        )

    # Rung 3: uv cache clean + reinstall
    pkg_names = [name for name, _ in _LAYER1_WHEEL_DEPS]
    rc_clean = _run_cache_clean(pkg_names)
    if rc_clean != 0 and verbose:
        print(
            "  ! uv cache clean returned non-zero; continuing anyway", file=sys.stderr
        )
    rc, err = _run_install()
    res.rung_reached = 3
    if rc != 0:
        res.errors.append(f"rung-3 failed (exit {rc}): {err[:300]}")
        return res
    stale = _check_stale()
    res.stale = stale
    res.success = not stale
    return res
