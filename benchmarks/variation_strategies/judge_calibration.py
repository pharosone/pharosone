"""Dual-judge calibration runner — can DeepSeek V4 Flash replace GLM 5.2 as the judge?

The upcoming example variation experiment uses judge = ``z-ai/glm-5.2`` (the dominant cost item).
This script measures, over a SHARED sample of REAL trial transcripts taken from existing ``*.eval``
logs, whether judge = ``deepseek/deepseek-v4-flash`` agrees with the GLM judge. Because the example
SUT brain is ALSO DeepSeek V4 Flash, this is DeepSeek-judging-DeepSeek — so the headline risk is
self-family LENIENCY (DeepSeek confirming FEWER genuine breaches than GLM).

CONTEXT-FRUGALITY (this script does ALL the heavy lifting; the agent never reads raw logs):
  * Phase ``sample`` scans the NEWEST few ``*.eval`` logs (not all of them), extracts only the
    minimal per-trial fields, STRATIFIES (~equal provisional-positive and negative trials, capped at
    ``--max-trials`` <= 200), and writes them to an on-disk JSONL sample. It prints compact counts
    only and writes a small summary JSON.
  * Phase ``judge`` reuses the engine's OWN two-pass batch judge (``scoring.batch_judge``) twice over
    the IDENTICAL per-probe batches — once with GLM, once with DeepSeek — feeds the aligned per-trial
    verdicts to the pure, unit-tested agreement math (``benchmarks.judge_calibration.agreement``),
    and writes ``reports/judge_calibration.{json,md}``.

The judge prompt / chunking / parsing are the engine's, unchanged — only the model id is swapped, so
any disagreement is the judge MODEL, not a different harness.

  # 0. offline: build the sample + see its composition + a cost estimate (no key, no spend):
  uv run python -m benchmarks.variation_strategies.judge_calibration --phase sample

  # 1. judge the sample with both models (key read from env / .env; never printed):
  uv run python -m benchmarks.variation_strategies.judge_calibration --phase judge

The OpenRouter key is read from the environment ONLY (``PE_LLM_KEY`` / ``PE_CLIENT_LLM_KEY`` /
``OPENROUTER_API_KEY``; a ``.env`` is loaded if present) and held in memory — never argv, never logged.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import time
from collections import Counter, defaultdict
from pathlib import Path

import re

from inspect_ai.log import read_eval_log

from probe_engine.corpus.loader import load_corpus
from probe_engine.scoring.batch_judge import (
    JUDGE_STATUS_APPLIED,
    JUDGE_STATUS_UNAVAILABLE,
    TrialRecord,
    batch_judge_with_status,
)
from probe_engine.targets.mock import _regex_sample

from benchmarks.judge_calibration.agreement import summarize, summarize_by_group

# Provider-prefixed OpenRouter slugs (the engine prepends provider="openrouter"; we do it here so the
# ids route to OpenRouter exactly as a real example run does). REFERENCE = GLM, TEST = DeepSeek.
DEFAULT_GLM_MODEL = "openrouter/z-ai/glm-5.2"
DEFAULT_DEEPSEEK_MODEL = "openrouter/deepseek/deepseek-v4-flash"

# Per-1M-token USD prices (OpenRouter list, Jun 2026 — mirrors the live variation runner).
PRICES: dict[str, tuple[float, float]] = {
    DEFAULT_GLM_MODEL: (1.40, 4.40),
    DEFAULT_DEEPSEEK_MODEL: (0.09, 0.18),
    "z-ai/glm-5.2": (1.40, 4.40),
    "deepseek/deepseek-v4-flash": (0.09, 0.18),
}

# Judge-token anchors for the cost projection (same constants the example cost model uses).
_JUDGE_BLOCK_TOK = 650      # tokens per trial block in a judge batch (transcript + tools + reply)
_JUDGE_TEMPLATE_TOK = 400   # judge pass-prompt scaffolding
_JUDGE_OUT_TOK = 90         # judge output (JSON verdict)

# Storage truncations >= the batch judge's own block truncation (transcript 2500 / tools 1500 /
# reply 1200), so the judge's truncation stays the binding one — the model sees identical input to a
# live run where the full text was present and then truncated.
_STORE_TRANSCRIPT = 3000
_STORE_TOOLS = 1800
_STORE_REPLY = 1500

# Verbatim assistant replies the OFFLINE deterministic targets emit. The mock tier (default
# `probe-engine run` + the whole test suite) and the deterministic adaptive attacker write these into
# .eval logs that are INDISTINGUISHABLE from a real bridge run by Inspect's model id alone (both are
# the `mockllm/model` placeholder). A real DeepSeek-SUT reply is varied natural language and matches
# NONE of these. We use them to SKIP synthetic trials so the calibration only judges genuine
# DeepSeek-authored transcripts (self-family leniency is meaningless on templated/regex-sample text).
# Prefixes cover targets/mock.py::_apply_outcome success strings + the control/plant/refusal strings
# and targets/adaptive.py's deterministic refusal.
_MOCK_REPLY_PREFIXES: tuple[str, ...] = (
    "Sure: ",                                       # _apply_outcome contains/secret_fragment success
    "Sure, here it is:",                            # secret_leaked success
    "Done — ",                                      # tool_called / state / authz / arg_injection success
    "Done.",                                        # generic success
    "Here is the requested sensitive data.",        # secret_leaked (no refs) success
    "Applied the change.",                          # state_changed success
    "Of course — the value is ",                    # cross_turn_contradiction success
    "Here you go:",                                 # output_pattern (insecure-output) success
    "Noted.",                                       # plant-turn acknowledgement
    "Understood. I will only do the requested task.",  # mock no-action
    "I can't act on that.",                         # mock default refusal
    "I won't proceed with that request.",           # adaptive.py deterministic refusal
)

# Fallback regex-oracle defaults the mock emits when _regex_sample yields nothing useful.
_MOCK_OUTPUT_PATTERN_DEFAULTS = ("javascript:alert(1)",)


def _is_synthetic_reply(oracle: str, args: dict, reply: str) -> bool:
    """True if ``reply`` is a DETERMINISTIC offline-target output (mock/adaptive), i.e. NOT a genuine
    DeepSeek-authored reply. Principled, per-oracle:

      * empty reply -> synthetic (no judge signal anyway);
      * output_pattern/code_pattern -> the mock emits ``_regex_sample(pattern)`` (or a known default),
        recomputed here and compared byte-for-byte;
      * a runaway 'AAAA...' block -> the runaway_output mock filler;
      * otherwise -> matches one of the mock/adaptive reply PREFIXES.

    Recomputing the mock's own output from the probe's oracle args is what makes this exact rather than
    a guess: e.g. secret_fragment ['AKIA','wJalr'] -> 'Sure: AKIA wJalr'; output_pattern -> 'it was good'."""
    r = (reply or "").strip()
    if not r:
        return True
    if oracle in ("output_pattern", "code_pattern"):
        sample = (_regex_sample(str(args.get("pattern", ""))) or "").strip()
        if sample and r == sample:
            return True
        if r in _MOCK_OUTPUT_PATTERN_DEFAULTS:
            return True
    if re.fullmatch(r"A{40,}", r):
        return True
    return any(r.startswith(p) for p in _MOCK_REPLY_PREFIXES)


def _read_key() -> str | None:
    """OpenRouter key from env ONLY (load .env first if present). Returns the key or None; the value
    is NEVER printed or written anywhere by this script."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:  # dotenv optional; env may already carry the key
        pass
    return (
        os.environ.get("PE_LLM_KEY")
        or os.environ.get("PE_CLIENT_LLM_KEY")
        or os.environ.get("OPENROUTER_API_KEY")
    )


# ----------------------------------------------------------------------------------------------
# Phase 1 — sampling from existing .eval logs (offline, no model call).
# ----------------------------------------------------------------------------------------------
def _semantic_index(corpus_dir: str) -> dict[str, dict]:
    """Map probe_id -> {question, threshold, oracle} for every probe that HAS a semantic check.

    Only semantic-check probes are calibration-relevant: without a judge_prompt the batch judge
    short-circuits to ``no_judge`` (success == binary, NO model call), so the two judges would agree
    trivially and tell us nothing. We restrict the sample to these probes."""
    index: dict[str, dict] = {}
    for probe in load_corpus(corpus_dir):
        sem = probe.evaluation.semantic
        if sem is None:
            continue
        binary = probe.evaluation.binary
        index[probe.id] = {
            "question": sem.judge_prompt,
            "threshold": float(sem.confidence_threshold),
            "oracle": binary.oracle if binary else "(no_binary)",
            "args": (binary.args or {}) if binary else {},
        }
    return index


def _iter_log_paths(logs_dir: str, max_logs: int) -> list[Path]:
    """Newest-first ``*.eval`` paths, capped at ``max_logs``. Newest-first deliberately PREFERS the
    most recent runs (the example DeepSeek-SUT work) and keeps us in small recent logs."""
    paths = sorted(Path(logs_dir).glob("*.eval"), key=lambda p: p.stat().st_mtime, reverse=True)
    return paths[:max_logs]


def _project_trial(meta: dict, sem: dict, probe_id: str, log_name: str, sut_model: str) -> dict:
    """Project one scored sample's metadata into the minimal calibration record."""
    return {
        "probe_id": probe_id,
        "oracle": sem["oracle"],
        "question": sem["question"],
        "threshold": sem["threshold"],
        "transcript": str(meta.get("transcript", ""))[:_STORE_TRANSCRIPT],
        "tool_calls": str(meta.get("tool_calls", ""))[:_STORE_TOOLS],
        "reply": str(meta.get("reply", ""))[:_STORE_REPLY],
        "binary_hit": bool(meta.get("binary_hit")),
        "log_file": log_name,
        "sut_model": sut_model,
    }


def build_sample(args) -> dict:
    """Scan newest logs, stratify into ~equal positive/negative buckets (cap ``--max-trials``), and
    write the JSONL sample + a compact summary. Returns the summary dict."""
    sem_index = _semantic_index(args.corpus)
    target_pos = args.target_positives
    positives: list[dict] = []
    negatives: list[dict] = []
    model_counts: Counter[str] = Counter()
    logs_used: set[str] = set()
    scanned = 0
    skipped_synthetic = 0

    for path in _iter_log_paths(args.logs_dir, args.max_logs):
        if len(positives) >= target_pos and len(negatives) >= target_pos:
            break
        try:
            log = read_eval_log(str(path))
        except Exception as exc:  # a corrupt/locked log must not abort the scan
            print(f"  WARN: skip unreadable log {path.name}: {type(exc).__name__}", flush=True)
            continue
        scanned += 1
        sut_model = getattr(log.eval, "model", "?") or "?"
        model_counts[sut_model] += 1
        for sample in log.samples or []:
            pid = sample.metadata.get("probe_id") if sample.metadata else None
            if pid not in sem_index or not sample.scores:
                continue
            sem = sem_index[pid]
            meta = next(iter(sample.scores.values())).metadata or {}
            reply = str(meta.get("reply", ""))
            # A deterministic offline-target reply (mock/adaptive) is not real DeepSeek -> drop it so
            # the calibration only sees genuine transcripts (self-family leniency needs real replies).
            if not args.include_mock and _is_synthetic_reply(sem["oracle"], sem.get("args", {}), reply):
                skipped_synthetic += 1
                continue
            rec = _project_trial(meta, sem, pid, path.name, sut_model)
            if rec["binary_hit"]:
                if len(positives) < target_pos:
                    positives.append(rec)
                    logs_used.add(path.name)
            else:
                negatives.append(rec)  # collect freely; downsample to balance after the scan

    # Stratify: keep all positives (<= target), match negatives to a comparable (balanced) count, and
    # hard-cap the total at --max-trials. Deterministic downsample so the sample is reproducible.
    rng = random.Random(args.seed)
    rng.shuffle(negatives)
    n_pos = len(positives)
    neg_budget = min(len(negatives), max(n_pos, 0), max(0, args.max_trials - n_pos))
    negatives = negatives[:neg_budget]

    for rec in positives:
        rec["label"] = "positive"
    for rec in negatives:
        rec["label"] = "negative"
    rows = positives + negatives
    for i, rec in enumerate(rows):
        rec["idx"] = i

    out = Path(args.sample_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as fh:
        for rec in rows:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    by_oracle: dict[str, dict] = defaultdict(lambda: {"total": 0, "positives": 0})
    for rec in rows:
        b = by_oracle[rec["oracle"]]
        b["total"] += 1
        b["positives"] += int(rec["label"] == "positive")

    summary = {
        "logs_dir": args.logs_dir,
        "max_logs": args.max_logs,
        "logs_scanned": scanned,
        "trials_skipped_synthetic": skipped_synthetic,
        "logs_used_for_sample": len(logs_used),
        "n_semantic_probes_in_corpus": len(sem_index),
        "total_trials": len(rows),
        "positives": n_pos,
        "negatives": len(negatives),
        "by_oracle": {k: dict(v) for k, v in sorted(by_oracle.items())},
        "probe_ids_in_sample": sorted({r["probe_id"] for r in rows}),
        "inspect_model_counts_over_kept_logs": dict(model_counts.most_common()),
        "sut_provenance_note": (
            "Kept logs are non-mock (real model/bridge) runs. Inspect records the bridge placeholder "
            "'mockllm/model' for in-process bridge runs, so the SUT brain (DeepSeek V4 Flash per the "
            "example harness default) is inferred from run provenance, not the log's model field."
        ),
        "sample_path": str(out),
    }
    Path(args.summary_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.summary_out).write_text(json.dumps(summary, indent=2, ensure_ascii=False))

    print(f"  scanned {scanned} log(s); skipped {skipped_synthetic} synthetic trial(s); "
          f"sample = {len(rows)} trials ({n_pos} positive / {len(negatives)} negative) "
          f"from {len(logs_used)} log(s)")
    print(f"  by oracle: { {k: v['total'] for k, v in summary['by_oracle'].items()} }")
    print(f"  wrote sample -> {out}  summary -> {args.summary_out}")
    return summary


# ----------------------------------------------------------------------------------------------
# Cost estimate (judge-only; printed before any spend).
# ----------------------------------------------------------------------------------------------
def _chunk_sizes(group_n: int, batch_size: int) -> list[int]:
    if batch_size and batch_size > 0:
        sizes = [batch_size] * (group_n // batch_size)
        if group_n % batch_size:
            sizes.append(group_n % batch_size)
        return sizes
    return [group_n] if group_n else []


def estimate_cost(rows: list[dict], glm_model: str, deepseek_model: str, batch_size: int,
                  flag_rate: float = 0.8) -> dict:
    """Project the (small) judge-only spend. PASS-1 runs over every chunk; PASS-2 re-sends a chunk
    only when PASS-1 flags it (we oversampled positives, so assume a high flag rate). Each model
    judges the SAME chunks, so the two estimates differ only by price."""
    groups: dict[str, int] = Counter(r["probe_id"] for r in rows)
    chunks: list[int] = []
    for _, n in groups.items():
        chunks.extend(_chunk_sizes(n, batch_size))

    pass1_in = sum(s * _JUDGE_BLOCK_TOK + _JUDGE_TEMPLATE_TOK for s in chunks)
    pass1_out = len(chunks) * _JUDGE_OUT_TOK
    pass2_in = flag_rate * pass1_in
    pass2_out = flag_rate * len(chunks) * _JUDGE_OUT_TOK
    tok_in = pass1_in + pass2_in
    tok_out = pass1_out + pass2_out

    def usd(model: str) -> float:
        pin, pout = PRICES.get(model, (0.0, 0.0))
        return tok_in / 1e6 * pin + tok_out / 1e6 * pout

    per_model = {glm_model: round(usd(glm_model), 4), deepseek_model: round(usd(deepseek_model), 4)}
    return {
        "n_trials": len(rows),
        "n_probe_groups": len(groups),
        "n_chunks_per_model": len(chunks),
        "batch_size": batch_size,
        "flag_rate_assumed": flag_rate,
        "approx_tokens_per_model": {"in": round(tok_in), "out": round(tok_out)},
        "per_model_usd": per_model,
        "total_usd": round(sum(per_model.values()), 4),
    }


# ----------------------------------------------------------------------------------------------
# Phase 2 — dual judging via the engine's own two-pass batch judge.
# ----------------------------------------------------------------------------------------------
def _group_rows(rows: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for rec in rows:
        groups[rec["probe_id"]].append(rec)
    return groups


async def _judge_with_model(groups: dict[str, list[dict]], model_id: str, api_key: str | None,
                            batch_size: int, timeout_s: float) -> dict[int, bool]:
    """Run the engine's two-pass batch judge per probe group (sequentially, so concurrency stays
    bounded to a single group's chunks) and return {trial idx -> verdict}.

    Aborts loudly if the FIRST group comes back UNAVAILABLE — that means the model id/key could not be
    resolved at all (a bad slug or key), which would otherwise masquerade as 'no hits'."""
    verdicts: dict[int, bool] = {}
    seen_applied = False
    for gi, (pid, recs) in enumerate(groups.items()):
        records = [
            TrialRecord(
                index=i,
                transcript=r["transcript"],
                tool_calls=r["tool_calls"],
                reply=r["reply"],
                binary_hit=bool(r["binary_hit"]),
            )
            for i, r in enumerate(recs)
        ]
        question = recs[0]["question"]
        threshold = float(recs[0].get("threshold", 0.7))
        result = await batch_judge_with_status(
            records,
            judge_prompt=question,
            confidence_threshold=threshold,
            model_id=model_id,
            api_key=api_key,
            batch_size=batch_size,
            timeout_s=timeout_s,
        )
        if result.status == JUDGE_STATUS_UNAVAILABLE and not seen_applied:
            raise RuntimeError(
                f"judge model {model_id!r} unavailable (could not resolve/run): {result.error}"
            )
        seen_applied = seen_applied or result.status == JUDGE_STATUS_APPLIED
        for i, r in enumerate(recs):
            verdicts[r["idx"]] = bool(result.mask[i])
    return verdicts


def run_dual_judge(args, rows: list[dict], api_key: str) -> dict:
    """Judge the sample twice (GLM then DeepSeek) over identical batches; return the aligned report."""
    os.environ["INSPECT_EVAL_MAX_CONNECTIONS"] = str(args.max_connections)
    groups = _group_rows(rows)

    t0 = time.time()
    glm_verdicts = asyncio.run(
        _judge_with_model(groups, args.glm_model, api_key, args.judge_batch_size, args.judge_timeout_s)
    )
    t_glm = time.time() - t0
    print(f"  GLM judged {len(glm_verdicts)} trials in {t_glm:.0f}s", flush=True)

    t1 = time.time()
    ds_verdicts = asyncio.run(
        _judge_with_model(groups, args.deepseek_model, api_key, args.judge_batch_size, args.judge_timeout_s)
    )
    t_ds = time.time() - t1
    print(f"  DeepSeek judged {len(ds_verdicts)} trials in {t_ds:.0f}s", flush=True)

    # Align by global trial idx (the SAME trials, same order) so every downstream number is meaningful.
    ordered = sorted(rows, key=lambda r: r["idx"])
    ref = [glm_verdicts[r["idx"]] for r in ordered]   # GLM = reference
    test = [ds_verdicts[r["idx"]] for r in ordered]   # DeepSeek = test
    oracles = [r["oracle"] for r in ordered]

    overall = summarize(ref, test)
    by_oracle = summarize_by_group(ref, test, oracles)

    disagreements: list[dict] = []
    for r, rb, tb in zip(ordered, ref, test):
        if rb != tb:
            snippet = (r["reply"] or r["transcript"] or "").strip().replace("\n", " ")
            disagreements.append({
                "probe_id": r["probe_id"],
                "oracle": r["oracle"],
                "glm": "hit" if rb else "no",
                "deepseek": "hit" if tb else "no",
                "direction": "deepseek_missed" if rb and not tb else "deepseek_extra",
                "snippet": snippet[:160],
            })

    return {
        "models": {"reference_glm": args.glm_model, "test_deepseek": args.deepseek_model},
        "judge_batch_size": args.judge_batch_size,
        "timings_s": {"glm": round(t_glm, 1), "deepseek": round(t_ds, 1)},
        "overall": overall.as_dict(),
        "by_oracle": {k: v.as_dict() for k, v in sorted(by_oracle.items())},
        "disagreements": disagreements,
    }


# ----------------------------------------------------------------------------------------------
# Recommendation + report writing.
# ----------------------------------------------------------------------------------------------
# A hit-delta within +/- this many trials (over a ~100-positive sample) is treated as noise, not
# leniency, when combined with a passing kappa.
_DELTA_NOISE_TOL = 2


def _recommendation(overall: dict, by_oracle: dict, kappa_bar: float) -> dict:
    """Data-driven verdict. Safe only if kappa > bar AND the net hit-delta is within noise AND no
    oracle shows leniency. Otherwise the verdict is driven by the FAILURE MODE, which for a security
    scanner is decisive: precision < recall means DeepSeek invents hits (false positives), while
    recall < precision means DeepSeek MISSES hits (false negatives) — the dangerous, self-family
    leniency direction. The naive 'GLM rechecks DeepSeek-positives' hybrid only fixes false positives,
    so when the failure is false negatives the honest options are KEEP GLM or a recall-preserving
    hybrid (GLM rechecks DeepSeek-NEGATIVES, since DeepSeek's positives are already high-precision)."""
    kappa = overall["cohens_kappa"]
    delta = overall["hit_delta_deepseek_minus_glm"]
    precision = overall["deepseek_precision_on_hit"]
    recall = overall["deepseek_recall_on_hit"]
    lenient_oracles = sorted(
        k for k, v in by_oracle.items()
        if v["hit_delta_deepseek_minus_glm"] < 0 and v["n"] >= 3
    )

    if kappa > kappa_bar and abs(delta) <= _DELTA_NOISE_TOL and not lenient_oracles:
        return {
            "verdict": "DEEPSEEK_SAFE",
            "kappa_bar": kappa_bar, "cohens_kappa": kappa,
            "net_hit_delta_deepseek_minus_glm": delta,
            "deepseek_precision": precision, "deepseek_recall": recall,
            "lenient_semantic_oracles": lenient_oracles,
            "rationale": (f"kappa={kappa:.3f} > {kappa_bar}, net hit-delta={delta:+d} (within noise), "
                          "and no oracle shows leniency: DeepSeek can replace GLM as the judge."),
            "hybrid_option": None,
        }

    false_negative_dominant = recall < precision
    missed = max(0, -delta)
    rationale = (
        f"Cohen's kappa={kappa:.3f} ({'above' if kappa > kappa_bar else 'at/below'} the {kappa_bar} bar), "
        f"but DeepSeek confirms {missed} FEWER genuine breaches than GLM (net hit-delta={delta:+d}; "
        f"precision={precision:.2f}, recall={recall:.2f}). "
    )
    if lenient_oracles:
        rationale += (f"The misses concentrate on semantic oracles {lenient_oracles} — the checks the judge "
                      "exists to adjudicate. ")
    if false_negative_dominant:
        rationale += ("This is the self-family-leniency direction (false negatives), and for a security "
                      "scanner a MISSED breach is the dangerous error. KEEP GLM for the example run.")
        hybrid = ("Recall-preserving cost option: run DeepSeek as a cheap pre-pass and escalate its "
                  f"NEGATIVES to GLM (DeepSeek's positives are already high-precision at {precision:.2f}, so "
                  "they need no recheck; its negatives hide the missed breaches). NB the naive "
                  "'GLM-rechecks-DeepSeek-positives' hybrid does NOT help here — the errors are false "
                  "negatives, not false positives.")
    else:
        rationale += ("DeepSeek over-counts (false positives), so a 'GLM-rechecks-DeepSeek-positives' "
                      "hybrid would recover precision while keeping DeepSeek's cheap recall.")
        hybrid = ("DeepSeek default + GLM escalation on DeepSeek-POSITIVES filters the false positives "
                  "cheaply.")
    return {
        "verdict": "KEEP_GLM",
        "kappa_bar": kappa_bar, "cohens_kappa": kappa,
        "net_hit_delta_deepseek_minus_glm": delta,
        "deepseek_precision": precision, "deepseek_recall": recall,
        "lenient_semantic_oracles": lenient_oracles,
        "rationale": rationale,
        "hybrid_option": hybrid,
    }


def write_reports(args, summary: dict, estimate: dict, judged: dict | None,
                  recommendation: dict | None, blocker: str | None) -> None:
    report = {
        "purpose": "Decide whether judge=deepseek/deepseek-v4-flash can replace judge=z-ai/glm-5.2 "
                   "for the example variation experiment (DeepSeek-judging-DeepSeek: watch leniency).",
        "sample_summary": summary,
        "cost_estimate": estimate,
        "blocker": blocker,
        "results": judged,
        "recommendation": recommendation,
    }
    Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_json).write_text(json.dumps(report, indent=2, ensure_ascii=False))
    _write_markdown(args.out_md, report)
    print(f"  wrote report -> {args.out_json}  and  {args.out_md}")


def _write_markdown(path: str, report: dict) -> None:
    s = report["sample_summary"]
    est = report["cost_estimate"]
    lines: list[str] = []
    lines.append("# Dual-judge calibration — GLM 5.2 (reference) vs DeepSeek V4 Flash (test)\n")
    lines.append(report["purpose"] + "\n")

    lines.append("## Sample composition\n")
    lines.append(f"- Total trials: **{s['total_trials']}**  "
                 f"(positives **{s['positives']}** / negatives **{s['negatives']}**)")
    lines.append(f"- Logs scanned: {s['logs_scanned']} (cap {s['max_logs']}); "
                 f"synthetic trials skipped: {s.get('trials_skipped_synthetic', 0)}; "
                 f"logs contributing trials: {s['logs_used_for_sample']}")
    lines.append(f"- Semantic-check probes in corpus: {s['n_semantic_probes_in_corpus']}; "
                 f"distinct probes in sample: {len(s['probe_ids_in_sample'])}")
    lines.append("- By oracle kind (total / positives):")
    for k, v in s["by_oracle"].items():
        lines.append(f"  - `{k}`: {v['total']} / {v['positives']}")
    lines.append(f"- SUT provenance: {s.get('sut_provenance_note', '')}")
    lines.append("")

    lines.append("## Cost estimate (judge-only, no SUT run)\n")
    lines.append(f"- {est['n_trials']} trials, {est['n_probe_groups']} probe groups, "
                 f"{est['n_chunks_per_model']} chunks/model (batch_size={est['batch_size']}, "
                 f"flag_rate={est['flag_rate_assumed']})")
    for m, c in est["per_model_usd"].items():
        lines.append(f"  - `{m}`: ~${c}")
    lines.append(f"- **Total calibration spend: ~${est['total_usd']}**\n")

    if report["blocker"]:
        lines.append("## BLOCKER\n")
        lines.append(report["blocker"] + "\n")

    judged = report["results"]
    if judged:
        o = judged["overall"]
        c = o["confusion_2x2"]
        lines.append("## Agreement (GLM = ground truth, DeepSeek = candidate)\n")
        lines.append(f"- N judged: **{o['n']}**  |  GLM hits: **{o['ref_hits_glm']}**  |  "
                     f"DeepSeek hits: **{o['test_hits_deepseek']}**")
        lines.append(f"- % agreement: **{o['percent_agreement']:.3f}**  |  "
                     f"Cohen's kappa: **{o['cohens_kappa']:.3f}**")
        lines.append(f"- DeepSeek positive-class precision/recall/F1: "
                     f"**{o['deepseek_precision_on_hit']:.3f}** / "
                     f"**{o['deepseek_recall_on_hit']:.3f}** / **{o['deepseek_f1_on_hit']:.3f}**")
        lines.append(f"- **Net hit-delta (DeepSeek − GLM): {o['hit_delta_deepseek_minus_glm']:+d}**  "
                     "(negative = DeepSeek confirms fewer = self-family leniency)")
        lines.append("")
        lines.append("| 2×2 confusion | DeepSeek=hit | DeepSeek=no |")
        lines.append("|---|---|---|")
        lines.append(f"| **GLM=hit** | {c['both_hit']} | {c['glm_only_deepseek_missed']} (DeepSeek missed) |")
        lines.append(f"| **GLM=no** | {c['deepseek_only_extra']} (DeepSeek extra) | {c['both_no']} |")
        lines.append("")

        lines.append("## Per-oracle-kind breakdown\n")
        lines.append("| oracle | n | GLM hits | DeepSeek hits | kappa | hit-delta |")
        lines.append("|---|---|---|---|---|---|")
        for k, v in judged["by_oracle"].items():
            lines.append(f"| `{k}` | {v['n']} | {v['ref_hits_glm']} | {v['test_hits_deepseek']} | "
                         f"{v['cohens_kappa']:.3f} | {v['hit_delta_deepseek_minus_glm']:+d} |")
        lines.append("")

        diss = judged["disagreements"]
        lines.append(f"## Disagreements ({len(diss)} total; first {min(len(diss), 25)} shown)\n")
        for d in diss[:25]:
            lines.append(f"- `{d['probe_id']}` [{d['oracle']}] GLM={d['glm']} DeepSeek={d['deepseek']} "
                         f"({d['direction']}): \"{d['snippet']}\"")
        lines.append("")

    rec = report["recommendation"]
    if rec:
        lines.append("## Recommendation\n")
        lines.append(f"- **{rec['verdict']}** — {rec['rationale']}")
        if rec.get("lenient_semantic_oracles"):
            lines.append(f"- Oracles where DeepSeek is lenient (delta<0, n>=3): "
                         f"{rec['lenient_semantic_oracles']}")
        if rec.get("hybrid_option"):
            lines.append(f"- Hybrid option: {rec['hybrid_option']}")
        lines.append("")

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines))


def _load_sample(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"sample not found: {path} — run `--phase sample` first.")
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _no_key_exit(args, summary: dict, estimate: dict) -> None:
    """No key in env: write the plan + cost estimate and print the exact command to re-run."""
    blocker = ("No OpenRouter key in env (PE_LLM_KEY / PE_CLIENT_LLM_KEY / OPENROUTER_API_KEY, or a "
               ".env). Calibration plan + cost estimate written; no model was called.")
    write_reports(args, summary, estimate, judged=None, recommendation=None, blocker=blocker)
    print("\nNO KEY — nothing spent. To run the dual judge, put OPENROUTER_API_KEY in .env (or env) "
          "and run:\n  uv run python -m benchmarks.variation_strategies.judge_calibration --phase judge")


def main() -> None:
    ap = argparse.ArgumentParser(description="Dual-judge calibration: GLM 5.2 vs DeepSeek V4 Flash.")
    ap.add_argument("--phase", choices=["sample", "estimate", "judge", "report", "all"], default="all")
    ap.add_argument("--logs-dir", default="logs")
    ap.add_argument("--corpus", default="corpus/probes")
    ap.add_argument("--max-logs", type=int, default=1500, help="newest logs to scan (cap)")
    ap.add_argument("--max-trials", type=int, default=200, help="hard cap on sampled trials")
    ap.add_argument("--include-mock", action="store_true",
                    help="do NOT skip deterministic offline-target (mock/adaptive) trials (default "
                         "skips them — they aren't DeepSeek-authored, so they can't test leniency)")
    ap.add_argument("--target-positives", type=int, default=100, help="provisional-positive trials to seek")
    ap.add_argument("--seed", type=int, default=0, help="deterministic negative downsample seed")
    ap.add_argument("--sample-out", default="reports/judge_calib_sample.jsonl")
    ap.add_argument("--summary-out", default="reports/judge_calib_sample_summary.json")
    ap.add_argument("--out-json", default="reports/judge_calibration.json")
    ap.add_argument("--out-md", default="reports/judge_calibration.md")
    ap.add_argument("--glm-model", default=DEFAULT_GLM_MODEL)
    ap.add_argument("--deepseek-model", default=DEFAULT_DEEPSEEK_MODEL)
    ap.add_argument("--judge-batch-size", type=int, default=8,
                    help="batch judge chunk size; both judges use it identically")
    ap.add_argument("--judge-timeout-s", type=float, default=60.0)
    ap.add_argument("--max-connections", type=int, default=4, help="inspect concurrent API calls (modest)")
    ap.add_argument("--kappa-bar", type=float, default=0.8, help="kappa threshold for the recommendation")
    args = ap.parse_args()

    print("=" * 88)
    print("DUAL-JUDGE CALIBRATION — GLM 5.2 (reference) vs DeepSeek V4 Flash (test)")
    print("=" * 88)

    if args.phase == "report":
        # Re-render the report (and recompute the recommendation) from an existing judged JSON, with
        # NO model calls — used to refine the recommendation/markdown without re-spending on judging.
        report = json.loads(Path(args.out_json).read_text())
        judged = report.get("results")
        if not judged:
            raise SystemExit(f"{args.out_json} has no judged results to re-render.")
        rec = _recommendation(judged["overall"], judged["by_oracle"], args.kappa_bar)
        write_reports(args, report["sample_summary"], report["cost_estimate"], judged, rec,
                      blocker=report.get("blocker"))
        print(f"  re-rendered: {rec['verdict']} — {rec['rationale']}")
        return

    if args.phase in ("sample", "all"):
        print("\n[sample] scanning logs ...", flush=True)
        summary = build_sample(args)
    else:
        summary = json.loads(Path(args.summary_out).read_text()) if Path(args.summary_out).exists() else {}

    rows = _load_sample(args.sample_out)
    estimate = estimate_cost(rows, args.glm_model, args.deepseek_model, args.judge_batch_size)
    print(f"\n[estimate] judge-only spend ~${estimate['total_usd']} "
          f"(GLM ~${estimate['per_model_usd'][args.glm_model]}, "
          f"DeepSeek ~${estimate['per_model_usd'][args.deepseek_model]}) over "
          f"{estimate['n_trials']} trials / {estimate['n_chunks_per_model']} chunks/model")

    if args.phase in ("sample", "estimate"):
        write_reports(args, summary, estimate, judged=None, recommendation=None, blocker=None)
        return

    if not rows:
        write_reports(args, summary, estimate, judged=None, recommendation=None,
                      blocker="Sample is EMPTY (no semantic-check positive/negative trials found). "
                              "Widen --max-logs or run the example harness to produce DeepSeek-SUT logs.")
        print("  BLOCKER: empty sample — see report.")
        return

    pos = summary.get("positives", sum(1 for r in rows if r.get("label") == "positive"))
    if pos == 0:
        write_reports(args, summary, estimate, judged=None, recommendation=None,
                      blocker="No provisional-POSITIVE trials in the sample — judges only matter on "
                              "candidate breaches. Widen --max-logs / produce more DeepSeek-SUT logs.")
        print("  BLOCKER: zero positives — see report.")
        return

    api_key = _read_key()
    if not api_key:
        _no_key_exit(args, summary, estimate)
        return

    print(f"\n[judge] key present; judging {len(rows)} trials with both models "
          f"(max_connections={args.max_connections}, batch_size={args.judge_batch_size}) ...", flush=True)
    judged = run_dual_judge(args, rows, api_key)
    recommendation = _recommendation(judged["overall"], judged["by_oracle"], args.kappa_bar)
    write_reports(args, summary, estimate, judged, recommendation, blocker=None)

    o = judged["overall"]
    print("\n" + "=" * 88)
    print(f"RESULT: kappa={o['cohens_kappa']:.3f}  %agree={o['percent_agreement']:.3f}  "
          f"hit-delta(DeepSeek−GLM)={o['hit_delta_deepseek_minus_glm']:+d}  "
          f"P/R/F1={o['deepseek_precision_on_hit']:.2f}/{o['deepseek_recall_on_hit']:.2f}/"
          f"{o['deepseek_f1_on_hit']:.2f}")
    print(f"RECOMMENDATION: {recommendation['verdict']} — {recommendation['rationale']}")
    print("=" * 88)


if __name__ == "__main__":
    main()
