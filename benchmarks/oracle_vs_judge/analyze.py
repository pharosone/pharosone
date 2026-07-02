"""Oracle-vs-Judge analyzer (offline-first; keyed labeling is gated).

Heavy lifting lives HERE (a script), never in the agent's context: it reads `*.eval` logs, computes
the metrics, and writes `reports/oracle_vs_judge.{json,md}` + a sample JSONL. The agent only reads
the small report the script writes.

  # Part A only (no key, no spend) — override/confusion analysis over a chosen, finished log set:
  uv run python -m benchmarks.oracle_vs_judge.analyze \
      --logs 'logs/2026-06-29T*.eval' --exclude 'logs/2026-06-30T*.eval'

  # Part B DRY RUN (default, no key) — also builds the stratified sample + prints the label plan/cost:
  uv run python -m benchmarks.oracle_vs_judge.analyze --logs 'logs/2026-06-30T*.eval' --part-b

  # Part B keyed labeling (opt-in; needs --yes AND an OpenRouter key in env / .env):
  PE_LLM_KEY=... uv run python -m benchmarks.oracle_vs_judge.analyze \
      --logs 'logs/2026-06-30T*.eval' --part-b --yes \
      --labeler-model openrouter/anthropic/claude-opus-4.8

The OpenRouter key is read from the env ONLY (`PE_LLM_KEY` / `PE_CLIENT_LLM_KEY` /
`OPENROUTER_API_KEY`; a `.env` is loaded if present) and held in memory — never argv, never logged.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import Counter
from pathlib import Path

from inspect_ai.log import read_eval_log

from probe_engine.corpus.loader import load_corpus

from benchmarks.oracle_vs_judge.extract import (
    TrialRow,
    iter_log_paths,
    parse_time,
    rows_from_eval_log,
)

try:  # dotenv is optional; the key may already be in the environment without it.
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - import guard
    load_dotenv = None
from benchmarks.oracle_vs_judge.labeler import (
    estimate_label_cost,
    independence_blocker,
    label_sample,
    select_reference_sample,
    semantic_index,
)
from benchmarks.oracle_vs_judge.metrics import (
    prf,
    prf_by_group,
    summarize_overrides,
    summarize_overrides_by_group,
)

# Oracle kinds where the judge ADDS semantic value (they fire on a defended agent's refusal text /
# any string match, so a binary hit is NOT proof of a breach), vs the high-precision side-effect
# oracles where a hit is a real action and the judge is mostly REDUNDANT. Used only to GROUP the
# per-oracle override numbers in the report — the per-kind table is always emitted in full too.
SEMANTIC_VALUE_ORACLES = frozenset({"prompt_leak", "contains", "output_pattern", "secret_fragment"})

DEFAULT_LABELER_MODEL = "openrouter/anthropic/claude-opus-4.8"
DEFAULT_JUDGE_MODEL = "z-ai/glm-5.2"            # the engine's judge in the example run (for independence)
DEFAULT_SUT_MODEL = "deepseek/deepseek-v4-flash"  # the example SUT brain (for independence)

# How the analyzer recovers each field — embedded in the report so a reader can sanity-check it.
PROVENANCE = {
    "oracle_kind": "regex `oracle=(\\w+)` over each sample score's `explanation` "
                   "(written by scoring.oracle.probe_oracle_scorer; corpus fallback by probe_id)",
    "binary_hit": "sample score metadata['binary_hit'] (the static oracle's provisional hit); "
                  "legacy lean logs lack it -> recovered from the score value and flagged "
                  "binary_from_value",
    "judge_success": "sample score metadata['success'] (overwritten by the judge in "
                     "run.executor._apply_batch_judge)",
    "judge_applied": "sample score metadata['judge_applied'] is True (a REAL judge verdict; False on "
                     "offline/no-judge/unavailable fallback where success == binary_hit)",
    "probe_id": "sample.metadata['probe_id'] (else parsed from sample.id '<probe_id>#<n>')",
    "part_a_filter": "override/confusion metrics count ONLY judge_applied==True trials",
}


def _read_key() -> str | None:
    """OpenRouter key from env ONLY (load .env first if present); never printed or written."""
    if load_dotenv is not None:
        load_dotenv()
    return (
        os.environ.get("PE_LLM_KEY")
        or os.environ.get("PE_CLIENT_LLM_KEY")
        or os.environ.get("OPENROUTER_API_KEY")
    )


# ----------------------------------------------------------------------------------------------
# Scan (offline): read the chosen logs once into Part-A accumulators + the Part-B judged pool.
# ----------------------------------------------------------------------------------------------
def scan(args, oracle_by_probe: dict[str, str] | None) -> tuple[list[TrialRow], dict]:
    """Read every chosen log ONCE. Returns (judged_pool, summary).

    `judged_pool` holds the FULL rows (with transcripts) for judge_applied==True trials — the Part-A
    metric set AND the Part-B sampling pool. Non-judged trials are only COUNTED (no transcripts kept),
    so memory stays bounded regardless of how many logs are scanned."""
    paths = iter_log_paths(
        args.logs, exclude_globs=args.exclude,
        since_ts=parse_time(args.since), until_ts=parse_time(args.until),
    )
    if args.max_logs:
        paths = paths[: args.max_logs]

    judged_pool: list[TrialRow] = []
    total_trials = 0
    judge_applied_false = 0
    binary_from_value = 0
    unverified = 0
    by_oracle_all: Counter[str] = Counter()
    by_oracle_judged: Counter[str] = Counter()
    sut_models: Counter[str] = Counter()
    logs_scanned = 0
    logs_unreadable = 0

    for path in paths:
        try:
            log = read_eval_log(str(path))
        except Exception as exc:  # a corrupt / mid-write / locked log must not abort the scan
            logs_unreadable += 1
            print(f"  WARN: skip unreadable log {path.name}: {type(exc).__name__}", flush=True)
            continue
        logs_scanned += 1
        sut_models[getattr(log.eval, "model", "?") or "?"] += 1
        for row in rows_from_eval_log(log, path.name, oracle_by_probe=oracle_by_probe):
            total_trials += 1
            by_oracle_all[row.oracle_kind] += 1
            if row.binary_from_value:
                binary_from_value += 1
            if row.judge_unverified:
                unverified += 1
            if row.judge_applied:
                judged_pool.append(row)
                by_oracle_judged[row.oracle_kind] += 1
            else:
                judge_applied_false += 1

    summary = {
        "logs_globs": list(args.logs),
        "exclude_globs": list(args.exclude),
        "logs_matched": len(paths),
        "logs_scanned": logs_scanned,
        "logs_unreadable": logs_unreadable,
        "total_trials": total_trials,
        "judge_applied_true": len(judged_pool),
        "judge_applied_false_offline_fallback": judge_applied_false,
        "judge_unverified_trials": unverified,
        "binary_recovered_from_value_legacy": binary_from_value,
        "trials_by_oracle_kind_all": dict(by_oracle_all.most_common()),
        "trials_by_oracle_kind_judge_applied": dict(by_oracle_judged.most_common()),
        "sut_model_counts": dict(sut_models.most_common()),
    }
    return judged_pool, summary


# ----------------------------------------------------------------------------------------------
# Part A — oracle x judge override / confusion analysis (judge_applied==True only).
# ----------------------------------------------------------------------------------------------
def part_a(judged_pool: list[TrialRow]) -> dict:
    oracle = [r.binary_hit for r in judged_pool]
    judge = [r.judge_success for r in judged_pool]
    kinds = [r.oracle_kind for r in judged_pool]
    value_group = [
        "judge_value(semantic)" if k in SEMANTIC_VALUE_ORACLES else "redundant(high_precision)"
        for k in kinds
    ]
    overall = summarize_overrides(oracle, judge)
    by_kind = summarize_overrides_by_group(oracle, judge, kinds)
    by_value = summarize_overrides_by_group(oracle, judge, value_group)
    return {
        "n_judge_applied_trials": len(judged_pool),
        "overall": overall.as_dict(),
        "by_oracle_kind": {k: v.as_dict() for k, v in sorted(by_kind.items())},
        "by_judge_value_group": {k: v.as_dict() for k, v in sorted(by_value.items())},
    }


# ----------------------------------------------------------------------------------------------
# Part B — stratified sample + (gated) independent reference labeling + PRF of oracle AND judge.
# ----------------------------------------------------------------------------------------------
def _write_sample(sample_rows: list[TrialRow], path: str) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as fh:
        for i, r in enumerate(sample_rows):
            rec = {
                "idx": i,
                "log_file": r.log_file,
                "probe_id": r.probe_id,
                "sample_id": r.sample_id,
                "epoch": r.epoch,
                "oracle_kind": r.oracle_kind,
                "binary_hit": r.binary_hit,
                "judge_success": r.judge_success,
                "disagreement": r.disagreement,
                "transcript": r.transcript,
                "tool_calls": r.tool_calls,
                "reply": r.reply,
            }
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _prf_block(sample_rows: list[TrialRow], labeled) -> dict:
    """Compute PRF of the static oracle AND the LLM judge against the independent reference label,
    overall and per oracle kind. `labeled` is the aligned list of LabeledTrial."""
    by_key = {lt.row.key(): lt for lt in labeled}
    aligned = [r for r in sample_rows if r.key() in by_key]
    ref = [by_key[r.key()].reference for r in aligned]
    oracle_pred = [r.binary_hit for r in aligned]
    judge_pred = [r.judge_success for r in aligned]
    kinds = [r.oracle_kind for r in aligned]
    return {
        "n_labeled": len(aligned),
        "reference_positives": sum(1 for x in ref if x),
        "oracle_vs_reference": prf(oracle_pred, ref).as_dict(),
        "judge_vs_reference": prf(judge_pred, ref).as_dict(),
        "oracle_vs_reference_by_oracle_kind": {
            k: v.as_dict() for k, v in sorted(prf_by_group(oracle_pred, ref, kinds).items())
        },
        "judge_vs_reference_by_oracle_kind": {
            k: v.as_dict() for k, v in sorted(prf_by_group(judge_pred, ref, kinds).items())
        },
    }


def part_b(args, judged_pool: list[TrialRow], api_key: str | None) -> dict:
    """Build the stratified sample, the cost estimate, and the independence check. Runs keyed
    labeling ONLY when `--yes` AND a key is present AND the labeler is independent; otherwise it is a
    dry run (plan + cost + the exact command to run later)."""
    sample_rows, composition = select_reference_sample(
        judged_pool, cap=args.sample_cap, seed=args.seed,
        judge_applied_only=not args.include_non_judged,
    )
    _write_sample(sample_rows, args.sample_out)
    cost = estimate_label_cost(
        sample_rows, args.labeler_model,
        price_in=args.labeler_price_in, price_out=args.labeler_price_out,
    )
    blocker = independence_blocker(args.labeler_model, args.judge_model, args.sut_model)

    block = {
        "labeler_model": args.labeler_model,
        "labeler_independent_of": {"judge": args.judge_model, "sut": args.sut_model},
        "independence_blocker": blocker,
        "sample_path": args.sample_out,
        "sample_composition": composition.as_dict(),
        "label_cost_estimate": cost,
        "labeled": False,
        "results": None,
    }

    run_keyed = args.yes and bool(api_key) and not blocker and bool(sample_rows)
    if not run_keyed:
        reasons = []
        if not args.yes:
            reasons.append("no --yes (default dry run)")
        if not api_key:
            reasons.append("no OpenRouter key in env/.env")
        if blocker:
            reasons.append(f"labeler not independent: {blocker}")
        if not sample_rows:
            reasons.append("empty sample (no judge_applied trials to label)")
        block["dry_run_reason"] = "; ".join(reasons)
        return block

    sem_index = semantic_index(args.corpus)
    os.environ["INSPECT_EVAL_MAX_CONNECTIONS"] = str(args.max_connections)
    labeled = asyncio.run(
        label_sample(
            sample_rows, sem_index, model_id=args.labeler_model, api_key=api_key,
            threshold=args.labeler_threshold, max_concurrency=args.max_connections,
            timeout_s=args.judge_timeout_s,
        )
    )
    block["labeled"] = True
    block["results"] = _prf_block(sample_rows, labeled)
    return block


# ----------------------------------------------------------------------------------------------
# Report writing.
# ----------------------------------------------------------------------------------------------
def write_reports(args, summary: dict, part_a_block: dict, part_b_block: dict | None) -> dict:
    report = {
        "purpose": "Compare the STATIC ORACLE (binary_hit) vs the LLM JUDGE (success) on existing "
                   ".eval logs (Part A: override/confusion), and score BOTH against an INDEPENDENT "
                   "reference label (Part B: precision/recall/F1).",
        "field_provenance": PROVENANCE,
        "scan_summary": summary,
        "part_a_oracle_vs_judge": part_a_block,
        "part_b_independent_reference": part_b_block,
    }
    Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_json).write_text(json.dumps(report, indent=2, ensure_ascii=False))
    _write_markdown(args.out_md, report)
    print(f"  wrote report -> {args.out_json}  and  {args.out_md}")
    return report


def _write_markdown(path: str, report: dict) -> None:
    s = report["scan_summary"]
    a = report["part_a_oracle_vs_judge"]
    b = report["part_b_independent_reference"]
    lines: list[str] = []
    lines.append("# Oracle vs Judge — override analysis + independent reference scoring\n")
    lines.append(report["purpose"] + "\n")

    lines.append("## Scan\n")
    lines.append(f"- Log globs: `{s['logs_globs']}`  (excluded: `{s['exclude_globs']}`)")
    lines.append(f"- Logs matched/scanned/unreadable: {s['logs_matched']} / {s['logs_scanned']} / "
                 f"{s['logs_unreadable']}")
    lines.append(f"- Trials: **{s['total_trials']}** total; "
                 f"**{s['judge_applied_true']}** judge_applied=True (informative); "
                 f"{s['judge_applied_false_offline_fallback']} offline-fallback "
                 f"(success==binary_hit, NOT informative)")
    lines.append(f"- Legacy lean-schema trials (binary recovered from score value): "
                 f"{s['binary_recovered_from_value_legacy']}; judge_unverified: "
                 f"{s['judge_unverified_trials']}")
    lines.append(f"- SUT model counts: {s['sut_model_counts']}")
    lines.append(f"- Trials by oracle kind (judge_applied): {s['trials_by_oracle_kind_judge_applied']}")
    lines.append("")

    lines.append("## Part A — oracle x judge override (judge_applied=True only)\n")
    o = a["overall"]
    lines.append(f"- N judged: **{o['n']}**  |  oracle hits: **{o['oracle_hits']}**  |  "
                 f"judge hits: **{o['judge_hits']}**")
    lines.append(f"- Agreement: **{o['agreement_rate']:.3f}**  |  override rate: "
                 f"**{o['override_rate']:.3f}**")
    lines.append(f"- Judge FILTERED (oracle=hit -> judge=no): **{o['fp_filtered_by_judge']}** "
                 f"({o['fp_filtered_rate_of_oracle_hits']:.3f} of oracle hits)")
    lines.append(f"- Judge CAUGHT (oracle=no -> judge=hit): **{o['fn_caught_by_judge']}** "
                 f"({o['fn_caught_rate_of_oracle_misses']:.3f} of oracle misses)")
    lines.append("")
    lines.append("| 2×2 (judged) | judge=hit | judge=no |")
    lines.append("|---|---|---|")
    c = o["confusion_2x2"]
    lines.append(f"| **oracle=hit** | {c['oracle_hit_judge_hit']} | "
                 f"{c['oracle_hit_judge_no']} (filtered FP) |")
    lines.append(f"| **oracle=no** | {c['oracle_no_judge_hit']} (caught FN) | "
                 f"{c['oracle_no_judge_no']} |")
    lines.append("")
    lines.append("### By judge-value group\n")
    lines.append("| group | n | agreement | overrides | filtered FP | caught FN |")
    lines.append("|---|---|---|---|---|---|")
    for k, v in a["by_judge_value_group"].items():
        lines.append(f"| {k} | {v['n']} | {v['agreement_rate']:.3f} | {v['overrides']} | "
                     f"{v['fp_filtered_by_judge']} | {v['fn_caught_by_judge']} |")
    lines.append("")
    lines.append("### By oracle kind\n")
    lines.append("| oracle | n | agreement | overrides | filtered FP | caught FN |")
    lines.append("|---|---|---|---|---|---|")
    for k, v in a["by_oracle_kind"].items():
        lines.append(f"| `{k}` | {v['n']} | {v['agreement_rate']:.3f} | {v['overrides']} | "
                     f"{v['fp_filtered_by_judge']} | {v['fn_caught_by_judge']} |")
    lines.append("")

    if b is not None:
        lines.append("## Part B — independent reference labeling\n")
        comp = b["sample_composition"]
        lines.append(f"- Labeler model: `{b['labeler_model']}` "
                     f"(independent of judge `{b['labeler_independent_of']['judge']}` "
                     f"& SUT `{b['labeler_independent_of']['sut']}`)")
        if b["independence_blocker"]:
            lines.append(f"- **INDEPENDENCE BLOCKER**: {b['independence_blocker']}")
        lines.append(f"- Sample: **{comp['total']}** trials from a pool of {comp['pool']} "
                     f"(taken {comp['taken']}; pool buckets {comp['pool_buckets']})")
        est = b["label_cost_estimate"]
        lines.append(f"- Label cost estimate: ~**${est['estimated_usd']}** over {est['calls']} calls "
                     f"(~{est['approx_tokens']['in']} in / {est['approx_tokens']['out']} out tok; "
                     f"price_known={est['price_known']})")
        lines.append(f"- Sample file: `{b['sample_path']}`")
        if not b["labeled"]:
            lines.append(f"- **DRY RUN** ({b.get('dry_run_reason', '')}) — no model called, "
                         "nothing spent.")
        else:
            r = b["results"]
            lines.append(f"- Labeled: **{r['n_labeled']}** (reference positives: "
                         f"{r['reference_positives']})")
            ov = r["oracle_vs_reference"]
            jv = r["judge_vs_reference"]
            lines.append("")
            lines.append("| predictor vs reference | P | R | F1 | acc | TP/FP/FN/TN |")
            lines.append("|---|---|---|---|---|---|")
            lines.append(f"| static oracle | {ov['precision']:.3f} | {ov['recall']:.3f} | "
                         f"{ov['f1']:.3f} | {ov['accuracy']:.3f} | "
                         f"{ov['tp']}/{ov['fp']}/{ov['fn']}/{ov['tn']} |")
            lines.append(f"| LLM judge | {jv['precision']:.3f} | {jv['recall']:.3f} | "
                         f"{jv['f1']:.3f} | {jv['accuracy']:.3f} | "
                         f"{jv['tp']}/{jv['fp']}/{jv['fn']}/{jv['tn']} |")
        lines.append("")

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines))


# ----------------------------------------------------------------------------------------------
def _oracle_by_probe(corpus_dir: str) -> dict[str, str]:
    """probe_id -> binary oracle kind, used only as a fallback when a score explanation somehow lacks
    `oracle=<kind>` (it shouldn't). Best-effort: a corpus that fails to load yields an empty map."""
    try:
        out: dict[str, str] = {}
        for probe in load_corpus(corpus_dir):
            binary = getattr(probe.evaluation, "binary", None)
            if binary is not None:
                out[probe.id] = binary.oracle
        return out
    except Exception as exc:
        print(f"  WARN: could not load corpus for oracle fallback ({type(exc).__name__}); "
              "relying on score explanations only", flush=True)
        return {}


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Oracle-vs-Judge analysis over existing .eval logs (Part A offline; Part B gated)."
    )
    ap.add_argument("--logs", nargs="+", default=["logs/*.eval"],
                    help="one or more globs of .eval logs to analyze (e.g. 'logs/2026-06-29T*.eval')")
    ap.add_argument("--exclude", nargs="*", default=[],
                    help="globs to EXCLUDE (e.g. an in-flight run: 'logs/2026-06-30T*.eval')")
    ap.add_argument("--since", default=None,
                    help="only logs with mtime >= this (ISO date/datetime or epoch secs) — the robust "
                         "way to isolate ONE run's fresh logs from older ones in the same logs/ dir")
    ap.add_argument("--until", default=None,
                    help="only logs with mtime <= this (ISO date/datetime or epoch secs)")
    ap.add_argument("--corpus", default="corpus/probes")
    ap.add_argument("--max-logs", type=int, default=0, help="cap on logs scanned (0 = no cap)")
    ap.add_argument("--part-b", action="store_true",
                    help="also build the Part-B stratified sample + cost estimate (and label if --yes)")
    ap.add_argument("--sample-cap", type=int, default=200, help="hard cap on Part-B sampled trials")
    ap.add_argument("--seed", type=int, default=0, help="deterministic sampling seed")
    ap.add_argument("--include-non-judged", action="store_true",
                    help="Part B: also sample judge_applied=False trials (default: judged-only)")
    ap.add_argument("--labeler-model", default=DEFAULT_LABELER_MODEL,
                    help="INDEPENDENT reference model (different family from judge GLM & SUT DeepSeek)")
    ap.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL, help="engine judge (independence check)")
    ap.add_argument("--sut-model", default=DEFAULT_SUT_MODEL, help="SUT brain (independence check)")
    ap.add_argument("--labeler-threshold", type=float, default=0.5,
                    help="confidence threshold the labeler uses to binarize its reference verdict")
    ap.add_argument("--labeler-price-in", type=float, default=None,
                    help="override $/1M input tokens for the labeler (else built-in price table)")
    ap.add_argument("--labeler-price-out", type=float, default=None,
                    help="override $/1M output tokens for the labeler")
    ap.add_argument("--max-connections", type=int, default=4, help="inspect concurrent API calls")
    ap.add_argument("--judge-timeout-s", type=float, default=60.0)
    ap.add_argument("--out-json", default="reports/oracle_vs_judge.json")
    ap.add_argument("--out-md", default="reports/oracle_vs_judge.md")
    ap.add_argument("--sample-out", default="reports/oracle_vs_judge_sample.jsonl")
    ap.add_argument("--yes", action="store_true",
                    help="actually call the labeler model (else Part B is a dry run)")
    args = ap.parse_args()

    print("=" * 88)
    print("ORACLE vs JUDGE — override analysis (Part A) + independent reference scoring (Part B)")
    print("=" * 88)

    oracle_by_probe = _oracle_by_probe(args.corpus)
    print(f"\n[scan] reading logs: {args.logs} (exclude {args.exclude}) ...", flush=True)
    judged_pool, summary = scan(args, oracle_by_probe)
    print(f"  scanned {summary['logs_scanned']} log(s); {summary['total_trials']} trials; "
          f"{summary['judge_applied_true']} judge_applied=True; "
          f"{summary['judge_applied_false_offline_fallback']} offline-fallback")

    part_a_block = part_a(judged_pool)
    oa = part_a_block["overall"]
    print(f"\n[part A] judged={oa['n']} agreement={oa['agreement_rate']:.3f} "
          f"overrides={oa['overrides']} (filteredFP={oa['fp_filtered_by_judge']}, "
          f"caughtFN={oa['fn_caught_by_judge']})")

    part_b_block = None
    if args.part_b:
        api_key = _read_key()
        part_b_block = part_b(args, judged_pool, api_key)
        comp = part_b_block["sample_composition"]
        est = part_b_block["label_cost_estimate"]
        print(f"\n[part B] sample={comp['total']} (dis={comp['taken']['disagreements']} "
              f"pos={comp['taken']['positive_agreements']} neg={comp['taken']['negative_agreements']}) "
              f"label cost ~${est['estimated_usd']}")
        if part_b_block["independence_blocker"]:
            print(f"  INDEPENDENCE BLOCKER: {part_b_block['independence_blocker']}")
        if not part_b_block["labeled"]:
            print(f"  DRY RUN: {part_b_block.get('dry_run_reason', '')} — nothing spent.")

    write_reports(args, summary, part_a_block, part_b_block)


if __name__ == "__main__":
    main()
