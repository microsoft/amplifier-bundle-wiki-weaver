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
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .policy import WikiPolicy, load_policy

# --------------------------------------------------------------------------
# Static asset locations (this repo).
# --------------------------------------------------------------------------

WIKI_WEAVER_ROOT = Path(__file__).resolve().parent.parent
PIPELINE_DIR = WIKI_WEAVER_ROOT / "pipeline"
INNER_DOT = PIPELINE_DIR / "wiki-weaver-inner.dot"
SCHEMA_PATH = PIPELINE_DIR / "SCHEMA.md"
VALIDATE_PY = PIPELINE_DIR / "validate_wiki.py"
NORMALIZE_PY = PIPELINE_DIR / "normalize_links.py"
# RESERVED FOR EVAL GRADING ONLY. The scenario rubric grades the WHOLE finished
# corpus wiki (all sources, the A/B test). It is the WRONG bar for the inner
# per-source loop: a single freshly-ingested article can never satisfy
# whole-corpus completeness, so assess would vote `refine` forever. Do NOT point
# the pipeline `assess` node at this -- it uses CONVERGENCE_RUBRIC_PATH below.
RUBRIC_PATH = WIKI_WEAVER_ROOT / "eval" / "scenario-01-llm-wiki" / "rubric.md"
# The pipeline `assess` gate's PER-SOURCE convergence rubric. Judges "is THIS
# one source well integrated?" -- the correct, achievable bar for the inner loop.
CONVERGENCE_RUBRIC_PATH = PIPELINE_DIR / "CONVERGENCE_RUBRIC.md"

# The attractor-pipeline bundle: composes the loop-pipeline orchestrator,
# context-simple, the anthropic provider, filesystem/bash/search tools, and the
# per-provider child agents the engine spawns. Local checkout preferred; the
# bundle's ``attractor:`` namespace resolves to the cached microsoft repo via
# the user registry. Falls back to the canonical git URL.
# Set WIKI_WEAVER_ATTRACTOR_PIPELINE to point at a local checkout of the
# attractor-pipeline bundle (e.g. the bundles/attractor-pipeline.yaml inside a
# local clone of amplifier-bundle-attractor). When the env var is absent the
# loader falls through to ATTRACTOR_PIPELINE_GIT below.
ATTRACTOR_PIPELINE_LOCAL = os.environ.get("WIKI_WEAVER_ATTRACTOR_PIPELINE")
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
MODEL = os.environ.get("WIKI_WEAVER_MODEL", "claude-sonnet-4-6")
# Optional per-node reasoning_effort (recognized stylesheet property). Unset =>
# omitted entirely, so default behaviour (e.g. Wave 1 anthropic) is unchanged.
REASONING_EFFORT = os.environ.get("WIKI_WEAVER_REASONING_EFFORT")

# Attractor namespace root (repo that owns ``attractor:agents/...`` refs). The
# per-provider agent bundles live under ``<root>/agents/<name>.yaml``.
# Set only when WIKI_WEAVER_ATTRACTOR_PIPELINE points at a local checkout.
_ATTRACTOR_REPO_ROOT: Path | None = (
    Path(ATTRACTOR_PIPELINE_LOCAL).resolve().parent.parent
    if ATTRACTOR_PIPELINE_LOCAL
    else None
)

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
    policy: WikiPolicy,
    source_id: int | str = "",
) -> str:
    """Read the inner DOT, substitute its required context variables with
    concrete ABSOLUTE paths, then inject ``llm_provider`` on each LLM node.

    ``policy`` — the resolved WikiPolicy for this wiki (from load_policy).
    Default policy (no project policy/ dir) produces byte-identical output
    to the pre-Phase-D code that used module-level constants.

    ``source_id`` is the stable id the CLI assigned to this source BEFORE
    ingest (Fix 3). It is injected as ``$source_id`` so the ingest node uses
    the authoritative id instead of guessing one per run.
    """
    import sys

    # Inner DOT source: project override when present, else engine default.
    dot = policy.inner_dot_path.read_text(encoding="utf-8")

    # The validator writes its structured PASS/FAIL result to this known file on
    # EVERY run (--out). The feedback + refine-ingest nodes are told to READ it,
    # so the deterministic failures are plumbed into the remediation path
    # (PIPELINE_DESIGN.md §4). Dotted context keys are silently dropped in
    # box-node prompts, so a file is the reliable hand-off channel.
    validation_report = wiki_dir / ".ai" / "validation.md"
    validate_cmd = (
        f"{sys.executable} {VALIDATE_PY} {wiki_dir} --out {validation_report}"
    )
    # Append --config when the wiki supplies a project validator config so that
    # the in-pipeline validate node uses the same constants as cmd_lint.
    if policy.validator_config_path is not None:
        validate_cmd += f" --config {policy.validator_config_path}"

    normalize_cmd = f"{sys.executable} {NORMALIZE_PY} {wiki_dir}"

    substitutions = {
        "$source_path": str(source_path),
        "$wiki_dir": str(wiki_dir),
        "$validation_report": str(validation_report),
        # Schema / rubric: project override or engine default (byte-identical
        # for default wikis because policy defaults == the original constants).
        "$schema_path": str(policy.schema_path),
        # assess uses the PER-SOURCE convergence rubric, NOT the whole-corpus
        # eval rubric (which would vote `refine` forever on a single article).
        "$convergence_rubric": str(policy.convergence_rubric_path),
        # $rubric_path retained for any eval-grading reuse; no longer referenced
        # by the inner pipeline's assess node (kept reserved for the eval grader).
        "$rubric_path": str(RUBRIC_PATH),
        "$normalize_cmd": normalize_cmd,
        "$validate_cmd": validate_cmd,
        "$max_cycles": str(policy.max_cycles),
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
    #
    # Per-node model: policy.model_for(nid) allows stage-level model tiering
    # (e.g. cheap model for feedback, strong model for assess).  For default
    # policy all stages resolve to the same model — byte-identical to pre-Phase-D.
    re_attr = f' reasoning_effort="{REASONING_EFFORT}",' if REASONING_EFFORT else ""
    for nid in LLM_NODE_IDS:
        nid_model = policy.model_for(nid)
        attrs = (
            f'        llm_provider="{policy.provider}", '
            f'llm_model="{nid_model}",{re_attr}\n'
        )
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
            candidates: list[str] = []
            if _ATTRACTOR_REPO_ROOT is not None:
                local = (_ATTRACTOR_REPO_ROOT / rel).with_suffix(".yaml")
                candidates.append(str(local))
            candidates.append(
                f"git+https://github.com/microsoft/amplifier-bundle-attractor@main"
                f"#subdirectory={rel}.yaml"
            )
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
# Ask pipeline: single-shot wiki reading + cited answer
# --------------------------------------------------------------------------
#
# MECHANISM (structural, not instructional): each spawned child agent has
#   - tool-bash + tool-web-* REMOVED from its tools list (structural exclusion)
#   - tool-filesystem denied_write_paths=["/"] (deny all writes)
#   - tool-filesystem root_path + allowed_read_paths = wiki_dir (constrain reads)
# This forces the agent to ground answers in wiki content — it structurally
# cannot write files, shell out, or fetch from the web.


@dataclass
class AskResult:
    """Outcome of a wiki-ask operation."""

    answer: str
    pages_used: list[str]
    refused: bool
    raw: str
    logs_dir: Path


def _dot_escape_prompt(s: str) -> str:
    """Escape a Python string for use as a DOT double-quoted attribute value.

    DOT escape conventions used by the loop-pipeline engine:
      \\  ->  \\\\  (backslashes first)
      "   ->  \\"   (embedded double quotes)
      newline -> \\n  (literal \\n in DOT file; engine reconstructs newline)
    """
    s = s.replace("\\", "\\\\")
    s = s.replace('"', '\\"')
    s = s.replace("\n", "\\n")
    return s


_ASK_ESCAPE_MODULES: frozenset[str] = frozenset(
    {"tool-bash", "tool-web-fetch", "tool-search-web", "tool-web", "tool-web-search"}
)


def _constrain_ask_agent(child_bundle: Any, wiki_dir: Path, answer_file: Path) -> None:
    """Scope the child agent to read-only wiki access (THE MECHANISM).

    Structural steps:
    1. Remove tool-bash / tool-web-* from the tools list — the agent CANNOT
       shell or fetch from the web. Works when tools are still in dict form
       (pre-prepare bundle). No-op if already resolved to module objects.
    2. Set tool-filesystem ``denied_write_paths=[wiki_dir]`` — deny writes to
       all wiki content (index, pages, .sources.json). The agent CAN write the
       answer to ``answer_file`` (a temp path outside wiki_dir) so the full
       response survives past the pipeline's notes truncation.
    3. Set ``root_path`` + ``allowed_read_paths`` on tool-filesystem to
       ``wiki_dir`` — constrains reads to wiki dir (best-effort; honoured if
       the module supports these config keys, silently ignored otherwise).

    HONEST LIMITATION: bash removal relies on dict-form tools. The write-deny
    constraint is the structural backstop regardless of tool form.
    """
    tools = getattr(child_bundle, "tools", None)
    if not isinstance(tools, list):
        return

    # Step 1: remove bash/web tools (structural).
    removals = [
        i
        for i, t in enumerate(tools)
        if isinstance(t, dict) and t.get("module", "") in _ASK_ESCAPE_MODULES
    ]
    for i in reversed(removals):
        tools.pop(i)

    # Step 2+3: scope filesystem tool.
    wiki_dir_s = str(wiki_dir)
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("module") != "tool-filesystem":
            continue
        cfg = tool.get("config")
        if not isinstance(cfg, dict):
            cfg = {}
            tool["config"] = cfg
        # Deny writes to wiki content (protects all wiki pages and metadata).
        # answer_file is outside wiki_dir so the agent can still write it.
        cfg["denied_write_paths"] = [wiki_dir_s]
        # Best-effort read scoping — silently ignored if unsupported.
        cfg["root_path"] = wiki_dir_s
        cfg["allowed_read_paths"] = [wiki_dir_s]


def make_ask_spawn_fn(prepared: Any, wiki_dir: Path, answer_file: Path):
    """Like make_spawn_fn but constrains each child to read-only wiki access.

    Registered as ``session.spawn`` for the ask pipeline so every sub-session
    (the single "answer" node) structurally cannot modify wiki content, shell,
    or fetch from the web. It can read within ``wiki_dir`` and write the answer
    to ``answer_file`` (a temp path outside wiki_dir).
    """
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

        # THE MECHANISM: constrain this child to read-only wiki access.
        _constrain_ask_agent(child_bundle, wiki_dir, answer_file)

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


def _parse_ask_output(text: str) -> tuple[str, list[str], bool]:
    """Extract (answer, pages_used, refused) from raw pipeline output.

    The loop-pipeline wraps the agent's response as:
      {"status": "success", "notes": "Plain text response: <agent text>", ...}
    or, if the agent output JSON directly:
      {"answer": "...", "pages_used": [...], "refused": false}

    Steps:
    1. Parse as JSON — if it has "notes" (pipeline wrapper), unwrap and recurse;
       if it has "answer" (direct ask result), return it.
    2. Search for the last ```json...``` fenced block.
    3. Search for the last JSON object containing an "answer" key.
    4. Fall back to the full text.
    """
    import json as _json

    # Step 1: try parsing as JSON (handles pipeline wrapper and direct answers).
    try:
        data = _json.loads(text)
        if isinstance(data, dict):
            if "answer" in data:
                # Direct ask-result JSON from the agent.
                return (
                    str(data["answer"]),
                    [str(p) for p in (data.get("pages_used") or [])],
                    bool(data.get("refused", False)),
                )
            if "notes" in data:
                # Loop-pipeline wrapper: agent response is in "notes".
                notes = str(data["notes"])
                # Strip "Plain text response: " prefix added by the pipeline.
                _PREFIX = "Plain text response: "
                if notes.startswith(_PREFIX):
                    notes = notes[len(_PREFIX) :]
                if notes:
                    return _parse_ask_output(notes)
    except (ValueError, TypeError):
        pass

    # Step 2: last ```json...``` fenced block.
    fenced = list(re.finditer(r"```json\s*(.*?)```", text, re.DOTALL))
    if fenced:
        json_str = fenced[-1].group(1).strip()
        try:
            data = _json.loads(json_str)
            if isinstance(data, dict) and "answer" in data:
                return (
                    str(data["answer"]),
                    [str(p) for p in (data.get("pages_used") or [])],
                    bool(data.get("refused", False)),
                )
        except (ValueError, TypeError):
            pass

    # Step 3: last JSON object containing an "answer" key.
    candidates = list(re.finditer(r'\{"answer".*?\}', text, re.DOTALL))
    if candidates:
        json_str = candidates[-1].group(0)
        try:
            data = _json.loads(json_str)
            if isinstance(data, dict) and "answer" in data:
                return (
                    str(data["answer"]),
                    [str(p) for p in (data.get("pages_used") or [])],
                    bool(data.get("refused", False)),
                )
        except (ValueError, TypeError):
            pass

    # Step 4: return the full text trimmed.
    return text.strip(), [], False


def build_ask_dot(
    wiki_dir: Path,
    question: str,
    answer_file: Path,
    *,
    provider: str = PROVIDER,
    model: str = MODEL,
) -> str:
    """Build the single-node DOT pipeline for a wiki ask query.

    The DOT has one LLM node ("answer") that is instructed to:
      - Read index.md + overview.md to route to relevant pages
      - Follow [[wikilinks]] 1-2 hops
      - Synthesize a cited answer from wiki content
      - Write the answer JSON to answer_file
      - FAIL LOUD if the wiki does not cover the question

    ``provider`` / ``model`` — resolved from load_policy(wiki_dir) by the
    caller so the wiki's model-tier knob (``models.ask`` / ``models.default``)
    applies to retrieval too.  Defaults are the module-level constants;
    byte-identical for default wikis.

    The agent writes its full answer to ``answer_file`` to bypass the
    loop-pipeline's notes-field truncation. The spawned agent's tools are
    constrained by make_ask_spawn_fn (wiki writes denied, bash/web removed).
    """
    wiki_abs = str(wiki_dir.resolve())
    sources_json = str(wiki_dir / ".sources.json")
    answer_file_s = str(answer_file)

    # Build the agent instruction (real Python newlines; _dot_escape_prompt
    # encodes them as \\n for the DOT attribute).
    prompt = (
        "You are a wiki reader. Answer the question ONLY from the compiled wiki.\n"
        "\n"
        f"WIKI DIRECTORY: {wiki_abs}\n"
        f"QUESTION: {question}\n"
        "\n"
        "PROCEDURE — follow in order:\n"
        f"1. Read {wiki_abs}/index.md to find pages relevant to the question.\n"
        f"2. Read {wiki_abs}/overview.md to understand the wiki scope.\n"
        "3. Read the 2-3 most relevant pages from the index.\n"
        "4. Follow [[wikilinks]] in those pages (up to 2 hops) for related content.\n"
        f"5. Read {sources_json} to resolve source IDs to author+URL for citations.\n"
        "\n"
        "ANSWER RULES:\n"
        "  COVERED: Write a direct cited answer. Name every wiki page used.\n"
        "  Cite as [Author, URL] from .sources.json; cite by page name if no\n"
        "  author/URL is available.\n"
        f"  NOT COVERED: Say EXACTLY: \"The wiki does not cover '{question}'."
        ' Pages consulted: [list pages you read]."\n'
        "  Do NOT use training knowledge. Do NOT refuse silently. FAIL LOUD.\n"
        "  PARTIAL: State clearly what IS and what IS NOT covered.\n"
        "  Ground EVERY claim in wiki content you actually read. No fabrication.\n"
        "\n"
        "REQUIRED OUTPUT STEP (mandatory — do this last, after your reasoning):\n"
        f"Write a JSON file to exactly this path: {answer_file_s}\n"
        "The file content must be a single JSON object:\n"
        '{"answer": "<full answer text>", "pages_used": ["page.md"], "refused": false}\n'
        "For refused: set refused=true, list examined pages in the answer field.\n"
        "The file MUST be written before you finish — this is how your answer is captured."
    )

    prompt_dot = _dot_escape_prompt(prompt)

    # Build DOT using plain string concatenation for lines with literal { }
    # to avoid Python f-string brace conflicts.
    lines = [
        "digraph ask_wiki {",
        '    graph [goal="Answer question from compiled wiki", default_fidelity="compact"]',
        '    start [shape=Mdiamond, label="Start"]',
        "    answer [",
        '        label="Read wiki and answer",',
        f'        llm_provider="{provider}",',
        f'        llm_model="{model}",',
        f'        prompt="{prompt_dot}"',
        "    ]",
        '    done [shape=Msquare, label="Done"]',
        "    start -> answer -> done",
        "}",
        "",
    ]
    return "\n".join(lines)


async def _run_ask_pipeline(
    dot_source: str,
    logs_dir: Path,
    wiki_dir: Path,
    answer_file: Path,
) -> tuple[str, dict[str, Any]]:
    """Run the ask pipeline with the read-only spawn capability wired in."""
    import json

    prepared = await _build_prepared(dot_source, logs_dir)
    session = await prepared.create_session(session_cwd=wiki_dir)
    # THE MECHANISM: register the ask-specific spawn so every child is
    # constrained (wiki writes denied, bash/web removed).
    session.coordinator.register_capability(
        "session.spawn", make_ask_spawn_fn(prepared, wiki_dir, answer_file)
    )

    async with session:
        raw = await session.execute("Run the pipeline")

    text = str(raw)
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            data = {"notes": text}
    except (json.JSONDecodeError, TypeError):
        data = {"notes": text}
    return text, data


def run_ask(
    wiki_dir: str | Path,
    question: str,
) -> AskResult:
    """Answer a question by reading the compiled wiki (no embeddings/RAG).

    Navigates index.md + [[wikilinks]] to find grounded content, synthesizes
    a cited answer, and explicitly refuses with a loud "the wiki does not cover X"
    if the topic is absent.

    The spawned agent session is CI-instrumented (inherits the hook from the
    composed bundle) so cost/token/artifact events are captured in
    ``<wiki_dir>/.runs/ask-<ts>/`` alongside the ask.dot and session logs.

    Tool scoping (THE MECHANISM — structural, not instructional):
      - tool-bash and web tools REMOVED from the spawned agent's tools list
      - tool-filesystem: denied_write_paths=[wiki_dir] (protect wiki content)
        + root_path + allowed_read_paths=wiki_dir (constrain reads)
    The agent cannot modify wiki content or escape to the web. It CAN write
    its full answer to a temp file (answer_file) outside wiki_dir so the
    response survives the loop-pipeline's notes-field truncation.
    """
    wiki_dir = Path(wiki_dir).resolve()
    if not wiki_dir.is_dir():
        raise FileNotFoundError(f"wiki dir not found: {wiki_dir}")

    import uuid

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    logs_dir = wiki_dir / ".runs" / f"ask-{timestamp}-{uuid.uuid4().hex[:8]}"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # The answer_file lives outside wiki_dir so the denied_write_paths
    # constraint does not block the agent from writing its answer.
    answer_file = Path(f"/tmp/wiki_ask_{timestamp}_{uuid.uuid4().hex[:8]}.json")

    # Resolve provider/model from the wiki's policy so the model-tier knob
    # (models.ask / models.default) applies to retrieval too.
    _ask_policy = load_policy(wiki_dir)
    dot_source = build_ask_dot(
        wiki_dir,
        question,
        answer_file,
        provider=_ask_policy.provider,
        model=_ask_policy.model_for("ask"),
    )
    (logs_dir / "ask.dot").write_text(dot_source, encoding="utf-8")

    raw_text, _data = asyncio.run(
        _run_ask_pipeline(dot_source, logs_dir, wiki_dir, answer_file)
    )

    # Primary: read from answer_file (full response; bypasses notes truncation).
    if answer_file.exists():
        try:
            file_json = answer_file.read_text(encoding="utf-8")
            answer, pages_used, refused = _parse_ask_output(file_json)
            # Copy to logs dir for posterity, then clean up.
            (logs_dir / "ask_answer.json").write_text(file_json, encoding="utf-8")
            answer_file.unlink(missing_ok=True)
            return AskResult(
                answer=answer,
                pages_used=pages_used,
                refused=refused,
                raw=file_json[:5000],
                logs_dir=logs_dir,
            )
        except (OSError, ValueError):
            pass

    # Fallback: extract from (truncated) pipeline result notes.
    answer, pages_used, refused = _parse_ask_output(raw_text)
    return AskResult(
        answer=answer,
        pages_used=pages_used,
        refused=refused,
        raw=raw_text[:5000],
        logs_dir=logs_dir,
    )


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
    candidates = [s for s in (ATTRACTOR_PIPELINE_LOCAL, ATTRACTOR_PIPELINE_GIT) if s]
    for src in candidates:
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
    max_cycles: int | None = None,
    source_id: int | str = "",
) -> InnerResult:
    """Run the inner convergence pipeline for ONE source through the engine.

    ``max_cycles`` — when set, overrides the wiki's wiki.config.yaml value
    (CLI flag beats config file).  When None, the policy resolves from config
    (default 3 if not configured).

    ``source_id`` is the stable id the CLI assigned to this source (Fix 3); it
    is threaded into the ingest node as ``$source_id``.
    """
    source_path = Path(source_path).resolve()
    wiki_dir = Path(wiki_dir).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"source not found: {source_path}")
    if not wiki_dir.is_dir():
        raise FileNotFoundError(f"wiki dir not found: {wiki_dir}")

    policy = load_policy(wiki_dir, cli_max_cycles=max_cycles)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    logs_dir = wiki_dir / ".runs" / timestamp
    logs_dir.mkdir(parents=True, exist_ok=True)

    dot_source = build_dot(source_path, wiki_dir, policy, source_id=source_id)
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


# --------------------------------------------------------------------------
# RAG baseline: single-shot read over RAW source articles (no wiki synthesis)
# --------------------------------------------------------------------------
#
# MECHANISM: identical to the ask pipeline — the spawned agent's tools are
# constrained by make_ask_spawn_fn so it structurally cannot write files, shell
# out, or fetch from the web. The ONLY difference from run_ask is the substrate:
#   ask  → reads the compiled wiki (index.md + [[wikilinks]] + ~150 concept pages)
#   rag  → reads the raw article pile (748 raw .md files, no index, no links)
#
# This keeps the ONE variable that matters: SYNTHESIS. Any quality/cost/latency
# delta is attributable to the compiled wiki vs the raw article pile — nothing else.


def build_rag_dot(
    articles_dir: Path,
    question: str,
    answer_file: Path,
    *,
    provider: str = PROVIDER,
    model: str = MODEL,
) -> str:
    """Build the single-node DOT pipeline for a naive-RAG query over raw articles.

    The agent has no index.md, no [[wikilinks]], and no pre-compiled structure.
    It must FIND relevant articles by exploring the raw pile via glob/read, then
    synthesize an answer from what it finds — mirroring lexical retrieval + generate.

    ``provider`` / ``model`` — optional overrides (default: module-level constants).
    Byte-identical for callers that don't pass them.

    The spawned agent's tools are constrained by make_ask_spawn_fn (same as ask):
    bash/web removed, writes denied, reads scoped to articles_dir.
    """
    articles_abs = str(articles_dir.resolve())
    answer_file_s = str(answer_file)

    prompt = (
        "You are a research assistant. Answer the question ONLY from the raw source"
        " articles provided.\n"
        "\n"
        f"ARTICLES DIRECTORY: {articles_abs}\n"
        f"QUESTION: {question}\n"
        "\n"
        "PROCEDURE \u2014 follow in order:\n"
        f"1. Use glob to list all .md files in {articles_abs}/ and scan filenames"
        " for keywords relevant to the question.\n"
        "2. Read the 3-5 most likely-relevant articles based on filename + question"
        " topic keywords.\n"
        "3. If those articles don\u2019t answer the question fully, try 2-3 more by"
        " filename relevance.\n"
        "4. Synthesize your answer from the article content you actually read.\n"
        "\n"
        "ANSWER RULES:\n"
        "  COVERED: Write a direct answer citing the article filename(s) you read.\n"
        "  Cite as [filename.md] for each article you drew from.\n"
        f"  NOT COVERED: Say EXACTLY: \"The source articles do not cover '{question}'."
        ' Articles consulted: [list files you read]."\n'
        "  Do NOT use training knowledge. Do NOT refuse silently. FAIL LOUD.\n"
        "  PARTIAL: State clearly what IS and what IS NOT covered.\n"
        "  Ground EVERY claim in article content you actually read. No fabrication.\n"
        "\n"
        "REQUIRED OUTPUT STEP (mandatory \u2014 do this last, after your reasoning):\n"
        f"Write a JSON file to exactly this path: {answer_file_s}\n"
        "The file content must be a single JSON object:\n"
        '{"answer": "<full answer text>", "pages_used": ["filename.md"], "refused": false}\n'
        "For refused: set refused=true, list examined files in the pages_used field.\n"
        "The file MUST be written before you finish \u2014 this is how your answer is captured."
    )

    prompt_dot = _dot_escape_prompt(prompt)

    lines = [
        "digraph rag_baseline {",
        '    graph [goal="Answer question from raw source articles", default_fidelity="compact"]',
        '    start [shape=Mdiamond, label="Start"]',
        "    answer [",
        '        label="Search articles and answer",',
        f'        llm_provider="{provider}",',
        f'        llm_model="{model}",',
        f'        prompt="{prompt_dot}"',
        "    ]",
        '    done [shape=Msquare, label="Done"]',
        "    start -> answer -> done",
        "}",
        "",
    ]
    return "\n".join(lines)


def run_rag(
    articles_dir: str | Path,
    question: str,
) -> AskResult:
    """Answer a question by searching raw source articles (naive RAG baseline).

    MECHANISM (structural, not instructional): identical to run_ask \u2014 bash/web
    tools removed, writes denied, reads scoped to articles_dir. The ONLY difference
    is the substrate: the agent reads the raw article pile instead of the compiled
    wiki. There is no index.md, no overview.md, no [[wikilinks]] \u2014 the agent must
    find relevant articles by exploring the directory directly.

    This is Variant B of the A/B comparison. Reuses _run_ask_pipeline and
    make_ask_spawn_fn unchanged \u2014 the substrate swap is the only variable.

    Run directories use a unique uuid suffix (``rag-<ts>-<uuid8>``) so parallel
    baseline runs never collide on the same checkpoint path.
    """
    articles_dir = Path(articles_dir).expanduser().resolve()
    if not articles_dir.is_dir():
        raise FileNotFoundError(f"articles dir not found: {articles_dir}")

    import uuid

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    uid8 = uuid.uuid4().hex[:8]
    logs_dir = articles_dir / ".runs" / f"rag-{timestamp}-{uid8}"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # answer_file outside articles_dir so the denied_write_paths constraint
    # (which denies writes to articles_dir) does not block writing the answer.
    answer_file = Path(f"/tmp/wiki_rag_{timestamp}_{uid8}.json")

    dot_source = build_rag_dot(articles_dir, question, answer_file)
    (logs_dir / "rag.dot").write_text(dot_source, encoding="utf-8")

    # _run_ask_pipeline is substrate-agnostic: it sets session_cwd + constrains
    # the spawn to the given directory. Passing articles_dir in place of wiki_dir
    # gives us the same structural mechanism on the raw article pile.
    raw_text, _data = asyncio.run(
        _run_ask_pipeline(dot_source, logs_dir, articles_dir, answer_file)
    )

    # Primary: read from answer_file (full response; bypasses notes truncation).
    if answer_file.exists():
        try:
            file_json = answer_file.read_text(encoding="utf-8")
            answer, pages_used, refused = _parse_ask_output(file_json)
            (logs_dir / "rag_answer.json").write_text(file_json, encoding="utf-8")
            answer_file.unlink(missing_ok=True)
            return AskResult(
                answer=answer,
                pages_used=pages_used,
                refused=refused,
                raw=file_json[:5000],
                logs_dir=logs_dir,
            )
        except (OSError, ValueError):
            pass

    # Fallback: extract from (truncated) pipeline result notes.
    answer, pages_used, refused = _parse_ask_output(raw_text)
    return AskResult(
        answer=answer,
        pages_used=pages_used,
        refused=refused,
        raw=raw_text[:5000],
        logs_dir=logs_dir,
    )


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
