# pyright: reportMissingImports=false
#!/usr/bin/env python3
"""Run the wiki-weaver INNER convergence pipeline through the attractor engine.

PROPER PreparedBundle path (no sys.path hacks, no raw mount-plan):

    base     = await load_bundle(<attractor-pipeline bundle>)
    dot_ovl  = Bundle(session={"orchestrator": {"module": "loop-pipeline",
                                                 "config": {"dot_source": <inner.dot>}}})
    ci_ovl   = Bundle(hooks=[{"module": "hook-context-intelligence", ...}])
    prepared = await base.compose(dot_ovl).compose(ci_ovl).prepare()
    session  = await prepared.create_session(session_cwd=<wiki_dir>)
    session.coordinator.register_capability("session.spawn", make_spawn_fn(prepared))
    async with session: raw = await session.execute("Run the pipeline")

``prepared.spawn`` composes parent -> child by default, so the context-intelligence
hook is inherited by every per-node sub-session automatically.

The OUTER corpus sweep is a plain Python loop in the CLI (see wiki_weaver.py).
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Static asset locations (this repo).
# --------------------------------------------------------------------------

WIKI_WEAVER_ROOT = Path(__file__).resolve().parent.parent
PIPELINE_DIR = WIKI_WEAVER_ROOT / "pipeline"
INNER_DOT = PIPELINE_DIR / "wiki-weaver-inner.dot"
SCHEMA_PATH = PIPELINE_DIR / "SCHEMA.md"
VALIDATE_PY = PIPELINE_DIR / "validate_wiki.py"
RUBRIC_PATH = WIKI_WEAVER_ROOT / "eval" / "scenario-01-llm-wiki" / "rubric.md"

# The attractor-pipeline bundle: composes the loop-pipeline orchestrator,
# context-simple, the anthropic provider, filesystem/bash/search tools, and the
# per-provider child agents the engine spawns. Local checkout preferred; the
# bundle's ``attractor:`` namespace resolves to the cached microsoft repo via
# the user registry. Falls back to the canonical git URL.
ATTRACTOR_PIPELINE_LOCAL = os.environ.get(
    "WIKI_WEAVER_ATTRACTOR_PIPELINE",
    "/home/bkrabach/dev/medium-tools-wiki/amplifier-bundle-attractor/"
    "bundles/attractor-pipeline.yaml",
)
ATTRACTOR_PIPELINE_GIT = (
    "git+https://github.com/microsoft/amplifier-bundle-attractor@main"
    "#subdirectory=bundles/attractor-pipeline.yaml"
)

# Context-intelligence hook module source (already installed in the amplifier
# venv; prepare() resolves it from cache).
CI_HOOK_SOURCE = (
    "git+https://github.com/microsoft/amplifier-bundle-context-intelligence@main"
    "#subdirectory=modules/hook-context-intelligence"
)

SETTINGS_PATH = Path(
    os.environ.get(
        "AMPLIFIER_SETTINGS", str(Path.home() / ".amplifier" / "settings.yaml")
    )
)

# Provider the inner LLM nodes route to (maps to attractor-agent-anthropic via
# the bundle's orchestrator ``profiles`` table).
PROVIDER = os.environ.get("WIKI_WEAVER_PROVIDER", "anthropic")
# Explicit model for the LLM nodes. The attractor child agents intentionally
# carry NO default_model ("no silent defaults"), so the spawning node must name
# one. The engine forwards it as a provider_preference to the child session.
MODEL = os.environ.get("WIKI_WEAVER_MODEL", "claude-sonnet-4-20250514")

# Attractor namespace root (repo that owns ``attractor:agents/...`` refs). The
# per-provider agent bundles live under ``<root>/agents/<name>.yaml``.
_ATTRACTOR_REPO_ROOT = Path(ATTRACTOR_PIPELINE_LOCAL).resolve().parent.parent

# LLM-driven node ids in the inner DOT (need an explicit llm_provider so the
# engine routes them to a child agent). Tool nodes (validate) and routing nodes
# (check) do not.
LLM_NODE_IDS = ("ingest", "assess", "feedback")


@dataclass
class InnerResult:
    """Outcome of one inner-pipeline run for a single source."""

    status: str
    converged: bool
    logs_dir: Path
    notes: str = ""
    failure_reason: str | None = None


# --------------------------------------------------------------------------
# settings loader: CI hook config from overrides.hook-context-intelligence
# --------------------------------------------------------------------------


def load_ci_config() -> dict[str, Any]:
    """Read the context-intelligence hook config from the user's settings.

    Reads ``overrides.hook-context-intelligence.config`` from
    ~/.amplifier/settings.yaml (server_url + api_key). Never hardcodes the key.
    Returns ``{}`` (hook still composed, fails soft) if absent/unreadable.
    """
    try:
        import yaml  # pyright: ignore[reportMissingModuleSource]
    except Exception:  # noqa: BLE001
        return {}
    if not SETTINGS_PATH.is_file():
        return {}
    try:
        data = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}
    overrides = (data.get("overrides") or {}).get("hook-context-intelligence") or {}
    cfg = overrides.get("config") or {}
    out: dict[str, Any] = {}
    if cfg.get("context_intelligence_server_url"):
        out["context_intelligence_server_url"] = str(
            cfg["context_intelligence_server_url"]
        )
    if cfg.get("context_intelligence_api_key"):
        out["context_intelligence_api_key"] = str(cfg["context_intelligence_api_key"])
    return out


# --------------------------------------------------------------------------
# DOT preparation: $var substitution + per-node provider injection
# --------------------------------------------------------------------------


def build_dot(
    source_path: Path,
    wiki_dir: Path,
    max_cycles: int,
    source_id: int | str = "",
) -> str:
    """Read the inner DOT, substitute its required context variables with
    concrete ABSOLUTE paths, then inject ``llm_provider`` on each LLM node.

    ``source_id`` is the stable id the CLI assigned to this source BEFORE
    ingest (Fix 3). It is injected as ``$source_id`` so the ingest node uses
    the authoritative id instead of guessing one per run.
    """
    import sys

    dot = INNER_DOT.read_text(encoding="utf-8")

    validate_cmd = f"{sys.executable} {VALIDATE_PY} {wiki_dir}"
    substitutions = {
        "$source_path": str(source_path),
        "$wiki_dir": str(wiki_dir),
        "$schema_path": str(SCHEMA_PATH),
        "$rubric_path": str(RUBRIC_PATH),
        "$validate_cmd": validate_cmd,
        "$max_cycles": str(max_cycles),
        "$source_id": str(source_id),
    }
    for var, value in substitutions.items():
        dot = dot.replace(var, value)

    # Inject explicit llm_provider + llm_model on each LLM node. The provider
    # routes the node to the matching child agent (provider->agent map lives in
    # the bundle's orchestrator config); the model is required because the
    # attractor child agents carry no default_model. Anchor on the line-leading
    # declaration so the [[wikilinks]] inside prompt strings are never mistaken
    # for attributes.
    attrs = f'        llm_provider="{PROVIDER}", llm_model="{MODEL}",\n'
    for nid in LLM_NODE_IDS:
        opener = f"    {nid} [\n"
        if opener in dot and "llm_provider" not in dot.split(opener, 1)[1][:200]:
            dot = dot.replace(opener, opener + attrs, 1)

    return dot


# --------------------------------------------------------------------------
# spawn capability (canonical impl, resolves from prepared.bundle.agents)
# --------------------------------------------------------------------------


async def _resolve_agent_bundle(agent_name: str, config: dict[str, Any]) -> Any:
    """Resolve a per-node agent into a full, self-contained child Bundle.

    The attractor-pipeline bundle declares its child agents as lazy references
    (``{"bundle": "attractor:agents/attractor-agent-anthropic"}``) which carry
    NO inline session. If we spawned an empty child and let ``compose`` fill the
    blanks, the child would INHERIT the parent's ``loop-pipeline`` orchestrator
    and re-run the whole DOT (infinite-ish recursion that falls back to the
    direct backend). So we ``load_bundle`` the referenced agent, which brings
    its own ``loop-agent`` orchestrator + tools; on spawn that orchestrator
    overrides the parent's ``loop-pipeline`` (session deep-merge, child wins)
    while the parent's context-intelligence hook is still inherited.
    """
    from amplifier_foundation import load_bundle

    ref = config.get("bundle") if isinstance(config, dict) else None
    if ref:
        # e.g. "attractor:agents/attractor-agent-anthropic"
        ns, _, rel = str(ref).partition(":")
        if rel:
            local = (_ATTRACTOR_REPO_ROOT / rel).with_suffix(".yaml")
            candidates = [
                str(local),
                f"git+https://github.com/microsoft/amplifier-bundle-attractor@main"
                f"#subdirectory={rel}.yaml",
            ]
        else:
            candidates = [str(ref)]
        last_err: Exception | None = None
        for src in candidates:
            try:
                return await load_bundle(src)
            except Exception as e:  # noqa: BLE001
                last_err = e
        raise RuntimeError(
            f"Could not resolve agent bundle '{agent_name}' ({ref}): {last_err}"
        )

    # Inline config form (already a full agent definition).
    from amplifier_foundation import Bundle

    return Bundle(
        name=agent_name,
        version="1.0.0",
        session=config.get("session", {}),
        providers=config.get("providers", []),
        tools=config.get("tools", []),
        hooks=config.get("hooks", []),
        instruction=config.get("instruction")
        or config.get("system", {}).get("instruction"),
    )


# --------------------------------------------------------------------------
# Fix 1 -- per-node filesystem isolation
# --------------------------------------------------------------------------
#
# Process state (the .processed.jsonl ledger and the _archive/ directory) is the
# deterministic CLI's EXCLUSIVE responsibility. A spawned LLM node must be
# PHYSICALLY UNABLE to write it. The tool-filesystem module honours a
# ``denied_write_paths`` config (DENY wins over ALLOW in is_path_allowed), so we
# inject the ledger + archive paths into every spawned child agent's filesystem
# tool config. This blocks the write_file / edit_file tools at the source.
#
# HONEST LIMITATION: tool-bash has no path-level sandbox (only command
# allow/deny lists), so a determined agent could still shell its way around the
# filesystem deny-list. That escape hatch is covered by the deterministic GUARD
# in cli/wiki_weaver.py (Fix 1b), which fails loud on any agent-written ledger
# line / archive move the CLI did not perform. Prevention here; safety net there.


def _denied_process_paths(wiki_dir: Path) -> list[str]:
    """The two process-state paths a spawned node must never write."""
    wiki_dir = Path(wiki_dir).resolve()
    return [str(wiki_dir / ".processed.jsonl"), str(wiki_dir / "_archive")]


def _constrain_agent_fs(child_bundle: Any, wiki_dir: Path) -> None:
    """Inject ``denied_write_paths`` for the ledger + archive into the child
    bundle's tool-filesystem config, in place. Idempotent.
    """
    denied = _denied_process_paths(wiki_dir)
    tools = getattr(child_bundle, "tools", None)
    if not isinstance(tools, list):
        return
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("module") != "tool-filesystem":
            continue
        cfg = tool.get("config")
        if not isinstance(cfg, dict):
            cfg = {}
            tool["config"] = cfg
        existing = cfg.get("denied_write_paths") or []
        merged = list(dict.fromkeys([*existing, *denied]))
        cfg["denied_write_paths"] = merged


def make_spawn_fn(prepared: Any, wiki_dir: Path | None = None):
    """Build the ``session.spawn`` capability for a prepared bundle.

    Each pipeline node spawns a full child sub-session built from one of the
    bundle's per-provider agents (resolved to its own ``loop-agent``
    orchestrator + tools). ``prepared.spawn`` composes parent -> child by
    default, so the context-intelligence hook is inherited by every child.

    When ``wiki_dir`` is provided, every spawned child agent's filesystem tool
    is constrained so it cannot write the ledger or _archive/ (Fix 1).
    """
    # Cache resolved agent bundles across spawns within one process.
    _agent_cache: dict[str, Any] = {}

    async def spawn_capability(
        agent_name: str,
        instruction: str,
        parent_session: Any,
        agent_configs: dict[str, dict[str, Any]],
        sub_session_id: str | None = None,
        orchestrator_config: dict[str, Any] | None = None,
        parent_messages: list[dict[str, Any]] | None = None,
        provider_preferences: list | None = None,
        self_delegation_depth: int = 0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if agent_name in agent_configs:
            config = agent_configs[agent_name]
        elif agent_name in prepared.bundle.agents:
            config = prepared.bundle.agents[agent_name]
        else:
            available = list(agent_configs.keys()) + list(prepared.bundle.agents.keys())
            raise ValueError(f"Agent '{agent_name}' not found. Available: {available}")

        if agent_name not in _agent_cache:
            _agent_cache[agent_name] = await _resolve_agent_bundle(agent_name, config)
        child_bundle = _agent_cache[agent_name]

        # Fix 1: physically deny the spawned node write access to the ledger and
        # _archive/. DENY beats ALLOW in the filesystem tool, so write_file /
        # edit_file targeting those paths are rejected at the tool boundary.
        if wiki_dir is not None:
            _constrain_agent_fs(child_bundle, wiki_dir)

        return await prepared.spawn(
            child_bundle=child_bundle,
            instruction=instruction,
            session_id=sub_session_id,
            parent_session=parent_session,
            orchestrator_config=orchestrator_config,
            parent_messages=parent_messages,
            provider_preferences=provider_preferences,
            self_delegation_depth=self_delegation_depth,
        )

    return spawn_capability


# --------------------------------------------------------------------------
# PreparedBundle build + run
# --------------------------------------------------------------------------

# In-process cache: load the base bundle once; install deps once, then reuse the
# offline path for subsequent sources in the same sweep.
_BASE_BUNDLE: Any = None
_DEPS_INSTALLED = False


async def _load_base() -> Any:
    global _BASE_BUNDLE
    if _BASE_BUNDLE is not None:
        return _BASE_BUNDLE
    from amplifier_foundation import load_bundle

    last_err: Exception | None = None
    for src in (ATTRACTOR_PIPELINE_LOCAL, ATTRACTOR_PIPELINE_GIT):
        try:
            _BASE_BUNDLE = await load_bundle(src)
            return _BASE_BUNDLE
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"Could not load attractor-pipeline bundle: {last_err}")


async def _build_prepared(dot_source: str, logs_dir: Path) -> Any:
    """Compose base + DOT overlay + CI hook overlay and prepare."""
    global _DEPS_INSTALLED
    from amplifier_foundation import Bundle

    base = await _load_base()

    dot_ovl = Bundle(
        name="wiki-weaver-runtime",
        version="1.0.0",
        session={
            "orchestrator": {
                "module": "loop-pipeline",
                "config": {
                    "dot_source": dot_source,
                    "logs_root": str(logs_dir),
                },
            },
        },
    )

    ci_cfg = load_ci_config()
    ci_ovl = Bundle(
        name="wiki-weaver-ci",
        version="1.0.0",
        hooks=[
            {
                "module": "hook-context-intelligence",
                "source": CI_HOOK_SOURCE,
                "config": ci_cfg,
            }
        ],
    )

    composed = base.compose(dot_ovl).compose(ci_ovl)

    # First prepare in this process resolves/installs modules; subsequent ones
    # take the offline path. Override with WIKI_WEAVER_INSTALL_DEPS=0/1.
    env = os.environ.get("WIKI_WEAVER_INSTALL_DEPS")
    if env is not None:
        install_deps = env not in ("0", "false", "False", "")
    else:
        install_deps = not _DEPS_INSTALLED

    prepared = await composed.prepare(install_deps=install_deps)
    _DEPS_INSTALLED = True
    return prepared


async def _run_pipeline(
    dot_source: str,
    logs_dir: Path,
    cwd: Path,
    execute_prompt: str = "Run the wiki-weaver inner pipeline",
    wiki_dir: Path | None = None,
) -> tuple[str, dict[str, Any]]:
    """Core proper-path runner shared by the inner pipeline and the thin slice.

    Returns ``(raw_text, parsed_json_or_fallback)``.

    ``wiki_dir`` (when set) is forwarded to the spawn capability so each
    per-node child agent's filesystem tool is denied write access to the
    ledger and _archive/ (Fix 1).
    """
    import json

    prepared = await _build_prepared(dot_source, logs_dir)
    session = await prepared.create_session(session_cwd=cwd)
    session.coordinator.register_capability(
        "session.spawn", make_spawn_fn(prepared, wiki_dir=wiki_dir)
    )

    async with session:
        raw = await session.execute(execute_prompt)

    text = str(raw)
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            data = {"status": "unknown", "notes": text}
    except (json.JSONDecodeError, TypeError):
        data = {"status": "unknown", "notes": text}
    return text, data


# --------------------------------------------------------------------------
# Public entrypoints
# --------------------------------------------------------------------------


def run_inner(
    source_path: str | Path,
    wiki_dir: str | Path,
    *,
    max_cycles: int = 3,
    source_id: int | str = "",
) -> InnerResult:
    """Run the inner convergence pipeline for ONE source through the engine.

    ``source_id`` is the stable id the CLI assigned to this source (Fix 3); it
    is threaded into the ingest node as ``$source_id``.
    """
    source_path = Path(source_path).resolve()
    wiki_dir = Path(wiki_dir).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"source not found: {source_path}")
    if not wiki_dir.is_dir():
        raise FileNotFoundError(f"wiki dir not found: {wiki_dir}")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    logs_dir = wiki_dir / ".runs" / timestamp
    logs_dir.mkdir(parents=True, exist_ok=True)

    dot_source = build_dot(source_path, wiki_dir, max_cycles, source_id=source_id)
    (logs_dir / "inner.dot").write_text(dot_source, encoding="utf-8")

    _text, data = asyncio.run(
        _run_pipeline(dot_source, logs_dir, wiki_dir, wiki_dir=wiki_dir)
    )
    status = data.get("status", "unknown")
    return InnerResult(
        status=status,
        converged=status == "success",
        logs_dir=logs_dir,
        notes=str(data.get("notes", ""))[:2000],
        failure_reason=data.get("failure_reason"),
    )


THIN_SLICE_DOT = """\
digraph engine_smoke {{
    graph [goal="Prove the PreparedBundle path spawns a tool-capable child session"]

    start [shape=Mdiamond, label="Start"]
    write [
        label="Write proof file",
        llm_provider="{provider}",
        prompt="Write the exact text 'engine-ok' (no quotes, no trailing newline) to the file {out_path} using your filesystem tools. Then reply with the single word DONE."
    ]
    done [shape=Msquare, label="Done"]

    start -> write -> done
}}
"""


def run_thin_slice(
    out_path: str | Path, cwd: str | Path | None = None
) -> dict[str, Any]:
    """THIN SLICE: a trivial one-box DOT that writes 'engine-ok' to ``out_path``
    through the full proper PreparedBundle path. Make-or-break proof.

    Returns a small dict with the raw engine text and the resolved out_path.
    """
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cwd = Path(cwd).resolve() if cwd else out_path.parent

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    logs_dir = out_path.parent / ".thin-runs" / timestamp
    logs_dir.mkdir(parents=True, exist_ok=True)

    dot_source = THIN_SLICE_DOT.format(provider=PROVIDER, out_path=str(out_path))
    (logs_dir / "thin.dot").write_text(dot_source, encoding="utf-8")

    text, _data = asyncio.run(
        _run_pipeline(dot_source, logs_dir, cwd, execute_prompt="Run the pipeline")
    )
    return {
        "out_path": str(out_path),
        "exists": out_path.is_file(),
        "content": out_path.read_text(encoding="utf-8") if out_path.is_file() else None,
        "logs_dir": str(logs_dir),
        "raw": text[:2000],
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Run the wiki-weaver inner pipeline.")
    sub = ap.add_subparsers(dest="cmd")

    p_inner = sub.add_parser("inner", help="run the inner pipeline for one source")
    p_inner.add_argument("source_path")
    p_inner.add_argument("wiki_dir")
    p_inner.add_argument("--max-cycles", type=int, default=3)

    p_thin = sub.add_parser("thin", help="run the thin-slice proof")
    p_thin.add_argument("out_path")

    args = ap.parse_args()

    if args.cmd == "thin":
        res = run_thin_slice(args.out_path)
        print(res)
        raise SystemExit(0 if res["exists"] and res["content"] == "engine-ok" else 1)

    # default: inner
    result = run_inner(args.source_path, args.wiki_dir, max_cycles=args.max_cycles)
    print(f"status={result.status} converged={result.converged}")
    print(f"logs={result.logs_dir}")
    if result.notes:
        print(f"notes={result.notes}")
    raise SystemExit(0 if result.converged else 1)
