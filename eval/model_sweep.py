#!/usr/bin/env python3
"""Model-swap eval harness for the wiki-weaver pipeline.

A SMALL re-runnable loop (not a framework). The pipeline reads
WIKI_WEAVER_PROVIDER / WIKI_WEAVER_MODEL and injects them per LLM node
(engine_runner.py:80,84), so each "variant" is just a (provider, model) pair.

Two subcommands:

  variant   Run ONE variant: init a fresh wiki, then ingest the fixed
            scenario articles ONE AT A TIME (sequential), capturing each
            ingest's stdout to ingest_<n>.log and its wall-time. Writes
            <variant_dir>/variant_meta.json. Designed to be launched once
            per variant and run in PARALLEL with sibling variants.

  grade     Grade every variant under an output root and write results.json.
            Pure-deterministic: imports the existing graders/validator. No LLM.

Usage:
  PY=/home/bkrabach/.local/share/uv/tools/amplifier/bin/python3
  $PY eval/model_sweep.py variant \
      --provider anthropic --model claude-sonnet-4-6 \
      --outroot /.../<TS> --slug claude-sonnet-4-6 \
      --repo /home/bkrabach/dev/medium-tools-wiki/wiki-weaver
  $PY eval/model_sweep.py grade --outroot /.../<TS>
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# --------------------------------------------------------------------------
# Fixed scenario: the 3 Karpathy articles, ingested IN THIS ORDER.
# --------------------------------------------------------------------------
ARTICLES_DIR = Path.home() / "medium_articles"
SCENARIO_ARTICLES = [
    "Andrej_Karpathy_Killed_RAG._Or_Did_He_The_LLM_Wiki_Pattern.md",
    "Andrej_Karpathy_Stopped_Using_AI_to_Write_Code._Hes_Using_It_to_Build_a_Second_Brain_Instead.md",
    "How_I_turned_Andrej_Karpathys_LLM_Wiki_into_a_tool_that_writes_wikis_from_code.md",
]
EXPECTED_SOURCE_IDS = [1, 2, 3]

# Reliability-proxy patterns (model-dependent assess-verdict flake).
EMPTY_SPAWN_PAT = "spawn returned empty output; falling back to direct tool loop"
NO_EDGE_PAT = "no_matching_edge"

INBOX = "_inbox"
ARCHIVE = "_archive"
LEDGER_NAME = ".processed.jsonl"


# ==========================================================================
# variant runner
# ==========================================================================
def run_variant(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    outroot = Path(args.outroot).resolve()
    variant_dir = outroot / args.slug
    wiki = variant_dir / "wiki"
    variant_dir.mkdir(parents=True, exist_ok=True)

    meta: dict = {
        "slug": args.slug,
        "provider": args.provider,
        "model": args.model,
        "wiki": str(wiki),
        "max_cycles": args.max_cycles,
        "timeout": args.timeout,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "articles": [],
    }
    meta_path = variant_dir / "variant_meta.json"

    def save() -> None:
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    save()

    # 1. init fresh wiki
    init = subprocess.run(
        [sys.executable, "-u", "-m", "cli", "init", str(wiki)],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    (variant_dir / "init.log").write_text(init.stdout + init.stderr, encoding="utf-8")
    if init.returncode != 0:
        meta["init_failed"] = True
        meta["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        save()
        return 1

    inbox = wiki / INBOX

    # 2. ingest each article ONE AT A TIME
    total_wall = 0.0
    for n, fname in enumerate(SCENARIO_ARTICLES, start=1):
        src = ARTICLES_DIR / fname
        art: dict = {"n": n, "file": fname}
        if not src.is_file():
            art["error"] = f"source article not found: {src}"
            meta["articles"].append(art)
            save()
            continue

        # Place exactly this one article into the inbox (converged prior
        # articles were moved to _archive by the CLI, so inbox holds only this).
        shutil.copy2(src, inbox / fname)

        env = dict(os.environ)
        env["WIKI_WEAVER_PROVIDER"] = args.provider
        env["WIKI_WEAVER_MODEL"] = args.model
        env["PYTHONUNBUFFERED"] = "1"

        cmd = [
            "timeout",
            str(args.timeout),
            sys.executable,
            "-u",
            "-m",
            "cli",
            "ingest",
            "--wiki",
            str(wiki),
            "--max-cycles",
            str(args.max_cycles),
        ]
        log_path = variant_dir / f"ingest_{n}.log"
        t0 = time.time()
        with log_path.open("w", encoding="utf-8") as lf:
            proc = subprocess.run(
                cmd, cwd=str(repo), env=env, stdout=lf, stderr=subprocess.STDOUT
            )
        wall = time.time() - t0
        total_wall += wall

        art["returncode"] = proc.returncode
        art["timed_out"] = proc.returncode == 124  # `timeout` exit code
        art["wall_seconds"] = round(wall, 1)
        art["log"] = log_path.name
        meta["articles"].append(art)
        save()

    meta["total_wall_seconds"] = round(total_wall, 1)
    meta["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save()
    return 0


# ==========================================================================
# grader
# ==========================================================================
def _count_in_logs(variant_dir: Path, needle: str) -> int:
    total = 0
    for log in sorted(variant_dir.glob("ingest_*.log")):
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        total += text.count(needle)
    return total


def _grade_one(variant_dir: Path) -> dict:
    # Import the EXISTING graders/validator (deterministic, no LLM).
    sys.path.insert(0, str(Path(__file__).resolve().parent))  # eval/
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root
    from grade_wiki import (  # type: ignore
        grade_converge,
        ledger_integrity,
        max_source_accrual,
        no_duplicate_pages,
    )
    from pipeline.validate_wiki import validate  # type: ignore

    meta_path = variant_dir / "variant_meta.json"
    meta = (
        json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    )
    wiki = Path(meta.get("wiki", str(variant_dir / "wiki")))

    out: dict = {
        "slug": variant_dir.name,
        "provider": meta.get("provider"),
        "model": meta.get("model"),
        "wiki": str(wiki),
        "wiki_exists": wiki.is_dir(),
        "articles": meta.get("articles", []),
        "total_wall_seconds": meta.get("total_wall_seconds"),
    }

    if not wiki.is_dir():
        out["error"] = "wiki dir missing"
        return out

    # --- validator (exit-code equivalent: passed bool) ---
    val = validate(wiki)
    out["validator_passed"] = bool(val.get("passed"))
    out["validator_failures"] = val.get("failures", [])
    out["page_count"] = val.get("page_count")

    # --- converged: how many of the 3 are archived + ledger converged=true ---
    rows: list[dict] = []
    led = wiki / LEDGER_NAME
    if led.is_file():
        for line in led.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    converged_ids = sorted(
        {
            int(r["source_id"])
            for r in rows
            if r.get("converged") is True and r.get("source_id") is not None
        }
    )
    out["converged_ids"] = converged_ids
    out["converged_count"] = len(converged_ids)

    arc = wiki / ARCHIVE
    archived = sorted(p.name for p in arc.iterdir()) if arc.is_dir() else []
    out["archived_count"] = len(archived)
    out["archived"] = archived

    # --- S-converge composite grader + ledger integrity ---
    sconv = grade_converge(wiki, EXPECTED_SOURCE_IDS)
    out["s_converge_passed"] = sconv.passed
    out["s_converge_failures"] = sconv.failures
    out["s_converge_notes"] = sconv.notes

    integ = ledger_integrity(wiki)
    out["integrity_passed"] = integ.passed
    out["integrity_failures"] = integ.failures

    # --- merge proof: which page accrued the most source ids ---
    page, ids = max_source_accrual(wiki)
    out["merge_page"] = page
    out["merge_sources"] = sorted(ids)
    out["merge_ok"] = len(ids) >= 2

    # --- duplicate concept pages ---
    dups = no_duplicate_pages(wiki)
    out["duplicate_pages"] = dups
    out["duplicate_page_count"] = len(dups)

    # --- reliability proxy from logs ---
    out["empty_spawn_count"] = _count_in_logs(variant_dir, EMPTY_SPAWN_PAT)
    out["no_matching_edge_count"] = _count_in_logs(variant_dir, NO_EDGE_PAT)

    return out


def grade(args: argparse.Namespace) -> int:
    outroot = Path(args.outroot).resolve()
    variants = []
    for d in sorted(outroot.iterdir()):
        if d.is_dir() and (d / "variant_meta.json").is_file():
            variants.append(_grade_one(d))

    results = {
        "outroot": str(outroot),
        "graded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "expected_source_ids": EXPECTED_SOURCE_IDS,
        "scenario_articles": SCENARIO_ARTICLES,
        "variants": variants,
    }
    results_path = outroot / "results.json"
    results_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"wrote {results_path}")
    for v in variants:
        print(
            f"  {v['slug']:<26} conv={v.get('converged_count')}/3 "
            f"val={'PASS' if v.get('validator_passed') else 'FAIL'} "
            f"merge={'ok' if v.get('merge_ok') else 'NO'} "
            f"dups={v.get('duplicate_page_count')} "
            f"empty_spawn={v.get('empty_spawn_count')} "
            f"no_edge={v.get('no_matching_edge_count')} "
            f"wall={v.get('total_wall_seconds')}s"
        )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="wiki-weaver model-swap eval harness")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pv = sub.add_parser("variant", help="run one (provider, model) variant")
    pv.add_argument("--provider", required=True)
    pv.add_argument("--model", required=True)
    pv.add_argument("--outroot", required=True)
    pv.add_argument("--slug", required=True)
    pv.add_argument("--repo", required=True)
    pv.add_argument("--max-cycles", type=int, default=5)
    pv.add_argument("--timeout", type=int, default=1200)
    pv.set_defaults(func=run_variant)

    pg = sub.add_parser("grade", help="grade all variants under an outroot")
    pg.add_argument("--outroot", required=True)
    pg.set_defaults(func=grade)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
