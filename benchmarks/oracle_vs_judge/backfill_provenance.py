"""Backfill ADDITIVE provenance metadata into ALREADY-WRITTEN `.eval` logs (Task 2, migration).

WHY.  The bridge tier runs Inspect on the `mockllm/model` placeholder (the real agent runs inside the
adapter, outside Inspect's model slot), so historical `.eval` logs record `eval-model = mockllm/model`
and do NOT self-document the real SUT/gen/judge models. Task 1 fixed this going forward (the engine
now stamps provenance into `eval.metadata`). This script fixes the PAST: it adds the same additive
`eval.metadata` to the study's existing logs via the `inspect_ai.log` API (`read_eval_log` -> set
`eval.metadata` -> `write_eval_log`). It NEVER touches scores, verdicts, samples, or the core
`eval-model` field.

CONTEXT FRUGALITY (mandatory).  This script does everything itself and prints only COMPACT COUNTS —
it never emits `.eval` contents. A prior worker died of resource_exhaustion by pulling log contents
into its context; do not do that. Read logs HERE, print counts.

SCOPE (attribution must be CERTAIN).  The study ran on 2026-06-30 as a 5-arm live variation
comparison, twice: the `default` variant (~12:37–16:38 local) then the `hardened` variant
(~16:38–20:35 local). We attribute each log ONLY by its file mtime window (`--since`/`--until`) and
the `--variant-split` cutoff. Facts that are UNIFORM across every arm and therefore CERTAIN are
stamped: `sut_model` (DeepSeek v4 Flash — held constant across all arms per the study), `variant`
(default/hardened by the split), `tier` (bridge), and the `eval_model_placeholder_note`. `judge_model`
(GLM 5.2) is stamped ONLY on logs that actually carry a real judge verdict (detected via each
sample's `judge_applied` marker); logs with no judge applied (e.g. a judge-off screening pass, or a
probe with no semantic check) are left without a judge_model and counted.

`gen_model` IS DELIBERATELY OMITTED by default.  The five arms differ in HOW variants were produced:
four static arms (`curated`/`tuple-curated`/`compat`/`naive`) use DETERMINISTIC recombination (NO LLM
generation); only the `llm` arm used GLM 5.2 (replayed from a frozen pack). Existing logs carry NO
arm marker, so per-log gen attribution is NOT available — stamping `gen_model=glm-5.2` on the ~4/5
static-arm logs would be a FALSE attribution. It is therefore left off (report shows the count).
`--stamp-gen-model` is available for a user who explicitly wants the study-level value written anyway.

SAFETY.  Idempotent (a log already carrying provenance is skipped; a value conflict is reported, never
clobbered). Additive only. Dry-run by DEFAULT (no `--apply` = no writes). Before batch-applying, the
FIRST patched log is round-tripped and verified (scores/samples byte-stable) — a failure ABORTS the
batch. Every applied write is then re-read and verified (sample count + per-sample score signature
unchanged, provenance keys present).

    # dry run (default; no writes) over the study window:
    uv run python -m benchmarks.oracle_vs_judge.backfill_provenance --logs 'logs/*.eval'

    # apply (offline, additive, idempotent, verified):
    uv run python -m benchmarks.oracle_vs_judge.backfill_provenance --logs 'logs/*.eval' --apply
"""

from __future__ import annotations

import argparse
import os
from collections import Counter
from pathlib import Path

from inspect_ai.log import read_eval_log, write_eval_log

from probe_engine.run.executor import _EVAL_MODEL_PLACEHOLDER_NOTE

from benchmarks.oracle_vs_judge.extract import iter_log_paths, parse_time

# Study defaults (2026-06-30 live variation study, local time). All overridable on the CLI.
DEFAULT_SINCE = "2026-06-30T12:37:00"
DEFAULT_UNTIL = "2026-06-30T20:35:00"
DEFAULT_SPLIT = "2026-06-30T16:38:00"
DEFAULT_SUT_MODEL = "deepseek/deepseek-v4-flash"
DEFAULT_GEN_MODEL = "z-ai/glm-5.2"
DEFAULT_JUDGE_MODEL = "z-ai/glm-5.2"
DEFAULT_TIER = "bridge"

# Keys that constitute "this log already carries provenance" — if all present, the log is left alone.
_CORE_KEYS = ("tier", "sut_model", "variant")

_BACKFILL_SOURCE = (
    "backfill_provenance.py: 2026-06-30 live variation study, attributed by file-mtime window "
    "(sut/variant/tier certain across all arms; judge_model on judged logs; gen_model arm-specific, omitted)"
)


# ---- pure helpers ----------------------------------------------------------------------------

def variant_for(mtime: float, split_ts: float, before: str, after: str) -> str:
    """Attribute the defense variant from the file mtime relative to the run split cutoff."""
    return before if mtime < split_ts else after


def build_target(*, tier: str, variant: str, sut_model: str, judge_model: str | None,
                 gen_model: str | None) -> dict:
    """The ADDITIVE provenance keys to ensure are present. Only CERTAIN keys are included:
    `judge_model` is passed as None when this log had no judge applied; `gen_model` as None unless
    the caller explicitly opted in (it is arm-specific and not per-log attributable)."""
    target: dict = {
        "tier": tier,
        "sut_model": sut_model,
        "variant": variant,
        "provenance_backfilled": True,
        "provenance_source": _BACKFILL_SOURCE,
    }
    note = _EVAL_MODEL_PLACEHOLDER_NOTE.get(tier)
    if note:
        target["eval_model_placeholder_note"] = note
    if judge_model is not None:
        target["judge_model"] = judge_model
    if gen_model is not None:
        target["gen_model"] = gen_model
    return target


def plan_metadata(existing: dict | None, target: dict) -> tuple[str, dict | None]:
    """Decide the idempotent, additive action for one log given its current `eval.metadata`.

    Returns (action, new_metadata):
      * "patch"          -> new_metadata is the (merged) dict to write.
      * "skip-identical" -> already carries every target key with identical values.
      * "skip-present"   -> already carries the CORE provenance keys (forward-stamped or a prior
                            backfill) with no conflict — left untouched (idempotent).
      * "conflict"       -> a target/CORE key already has a DIFFERENT value — never clobbered.
    """
    if existing:
        # A differing value on any key we intend to write is a hard conflict — never overwrite.
        conflicts = [k for k, v in target.items() if k in existing and existing[k] != v]
        if conflicts:
            return "conflict", None
        # Already fully provenance'd (all core keys present, no conflict) -> leave it alone.
        if all(k in existing for k in _CORE_KEYS):
            if all(existing.get(k) == v for k, v in target.items()):
                return "skip-identical", existing
            return "skip-present", existing
        merged = {**existing, **target}
        return "patch", merged
    return "patch", dict(target)


def log_is_judged(log) -> bool:
    """True iff at least one sample carries a REAL judge verdict (`judge_applied is True`)."""
    for sample in log.samples or []:
        for sc in (sample.scores or {}).values():
            if (sc.metadata or {}).get("judge_applied") is True:
                return True
    return False


def score_signature(log) -> tuple:
    """A stable signature of everything that MUST be preserved (samples + scores). Compared before
    and after a write to prove the migration changed nothing but the eval metadata."""
    rows: list[tuple] = []
    for sample in log.samples or []:
        for name in sorted((sample.scores or {})):
            sc = sample.scores[name]
            meta = sc.metadata or {}
            rows.append((
                str(getattr(sample, "id", "")), int(getattr(sample, "epoch", 1) or 1), name,
                repr(sc.value), bool(meta.get("success")), meta.get("judge_applied"),
                bool(meta.get("binary_hit")) if "binary_hit" in meta else None,
            ))
    rows.sort()
    return (len(log.samples or []), tuple(rows))


# ---- per-log processing --------------------------------------------------------------------

def _write_and_verify(path: Path, new_metadata: dict) -> tuple[bool, str]:
    """Set `eval.metadata`, write, re-read, and verify the scores/samples are byte-stable and the
    provenance keys are present. Returns (ok, detail). Never raises — a failure is reported."""
    try:
        log = read_eval_log(str(path))
    except Exception as exc:  # unreadable/locked — report, never crash the batch
        return False, f"reread-before-failed:{type(exc).__name__}"
    before = score_signature(log)
    # Preserve the original file mtime: it is the study's ATTRIBUTION signal (and its true run time).
    # `write_eval_log` would otherwise reset it to "now", drifting the log out of the window and
    # breaking both re-attribution and idempotent re-runs.
    orig = path.stat()
    log.eval.metadata = new_metadata
    try:
        write_eval_log(log, str(path))
    except Exception as exc:
        return False, f"write-failed:{type(exc).__name__}"
    try:
        os.utime(path, (orig.st_atime, orig.st_mtime))
    except OSError:
        pass  # best-effort; a failed mtime-restore never invalidates the (correct) write
    try:
        log2 = read_eval_log(str(path))
    except Exception as exc:
        return False, f"reread-after-failed:{type(exc).__name__}"
    after = score_signature(log2)
    if before != after:
        return False, "SCORE/SAMPLE SIGNATURE CHANGED"
    missing = [k for k in new_metadata if (log2.eval.metadata or {}).get(k) != new_metadata[k]]
    if missing:
        return False, f"provenance-not-persisted:{missing}"
    if log2.eval.model != log.eval.model:
        return False, "eval-model CHANGED"
    return True, "ok"


def run(args) -> dict:
    since_ts = parse_time(args.since)
    until_ts = parse_time(args.until)
    split_ts = parse_time(args.variant_split)
    gen_to_stamp = args.gen_model if args.stamp_gen_model else None

    paths = iter_log_paths(args.logs, exclude_globs=args.exclude,
                           since_ts=since_ts, until_ts=until_ts, newest_first=False)
    if args.max_logs:
        paths = paths[: args.max_logs]

    counts: Counter[str] = Counter()
    by_variant: Counter[str] = Counter()
    examples: dict[str, list[str]] = {"patch": [], "skip": [], "conflict": [], "non_placeholder": [],
                                      "unjudged": [], "unreadable": []}
    to_patch: list[tuple[Path, dict]] = []

    for path in paths:
        counts["in_window"] += 1
        try:
            log = read_eval_log(str(path))
        except Exception:
            counts["unreadable"] += 1
            if len(examples["unreadable"]) < 5:
                examples["unreadable"].append(path.name)
            continue
        # Safety: only touch placeholder logs (the bridge/mock artifact). A real eval-model in the
        # window is a different run whose SUT we cannot assert — skip + report.
        if args.require_eval_model and log.eval.model != args.require_eval_model:
            counts["non_placeholder"] += 1
            if len(examples["non_placeholder"]) < 5:
                examples["non_placeholder"].append(f"{path.name}[{log.eval.model}]")
            continue

        variant = variant_for(path.stat().st_mtime, split_ts, args.variant_before, args.variant_after)
        by_variant[variant] += 1
        judged = log_is_judged(log)
        if not judged:
            counts["unjudged_no_judge_model"] += 1
            if len(examples["unjudged"]) < 5:
                examples["unjudged"].append(path.name)
        target = build_target(
            tier=args.tier, variant=variant, sut_model=args.sut_model,
            judge_model=(args.judge_model if judged else None), gen_model=gen_to_stamp,
        )
        action, new_meta = plan_metadata(log.eval.metadata, target)
        if action == "patch":
            counts["would_patch"] += 1
            counts[f"would_patch_{variant}"] += 1
            to_patch.append((path, new_meta))
            if len(examples["patch"]) < 6:
                examples["patch"].append(f"{path.name} -> variant={variant} judged={judged}")
        elif action in ("skip-identical", "skip-present"):
            counts["skip_already_provenanced"] += 1
            if len(examples["skip"]) < 5:
                examples["skip"].append(f"{path.name}[{action}]")
        else:  # conflict
            counts["conflict_left_untouched"] += 1
            if len(examples["conflict"]) < 5:
                examples["conflict"].append(path.name)

    _print_plan(args, counts, by_variant, examples, gen_to_stamp)

    if not args.apply:
        print("\nDRY RUN — no files written. Re-run with --apply to patch the 'would_patch' set.")
        return {"dry_run": True, "counts": dict(counts)}

    if not to_patch:
        print("\nnothing to patch (all in-scope logs already provenance'd or skipped).")
        return {"dry_run": False, "applied": 0, "counts": dict(counts)}

    # Back-safety GATE: prove the round-trip preserves everything on the FIRST log before the batch.
    first_path, first_meta = to_patch[0]
    ok, detail = _write_and_verify(first_path, first_meta)
    if not ok:
        print(f"\nABORT — round-trip safety check FAILED on {first_path.name}: {detail}. "
              "No further files were written.")
        return {"dry_run": False, "applied": 0, "aborted": True, "detail": detail}
    print(f"\n[apply] safety gate PASSED on {first_path.name} (scores/samples byte-stable, "
          "provenance persisted).")

    applied = 1
    failures: list[str] = []
    for path, meta in to_patch[1:]:
        ok, detail = _write_and_verify(path, meta)
        if ok:
            applied += 1
        else:
            failures.append(f"{path.name}:{detail}")
        if applied % max(1, args.progress_every) == 0:
            print(f"  [apply] {applied}/{len(to_patch)} patched+verified ...", flush=True)
        if failures and len(failures) >= args.max_failures:
            print(f"\nABORT — {len(failures)} verification failure(s) hit the cap; stopping. "
                  f"first few: {failures[:5]}")
            break

    print(f"\n[apply] DONE: patched+verified {applied}/{len(to_patch)} logs; "
          f"failures={len(failures)}")
    if failures:
        print(f"  failures (first 5): {failures[:5]}")
    # Final independent re-read verification on a sample of the patched set.
    _final_sample_verify(to_patch[:1] + to_patch[max(1, len(to_patch) - 2):], args)
    return {"dry_run": False, "applied": applied, "failures": len(failures),
            "counts": dict(counts)}


def _final_sample_verify(sample: list[tuple[Path, dict]], args) -> None:
    print("\n[verify] re-reading a sample of patched logs (independent confirmation):")
    for path, meta in sample:
        try:
            log = read_eval_log(str(path))
        except Exception as exc:
            print(f"  {path.name}: REREAD FAILED ({type(exc).__name__})")
            continue
        md = log.eval.metadata or {}
        present = all(md.get(k) == v for k, v in meta.items())
        print(f"  {path.name}: eval.model={log.eval.model!r}  samples={len(log.samples or [])}  "
              f"provenance_present={present}  sut_model={md.get('sut_model')!r} "
              f"variant={md.get('variant')!r} judge_model={md.get('judge_model')!r}")


def _print_plan(args, counts: Counter, by_variant: Counter, examples: dict,
                gen_to_stamp: str | None) -> None:
    print("=" * 92)
    print("BACKFILL PROVENANCE — additive eval.metadata for existing .eval logs (offline, scoped)")
    print("=" * 92)
    print(f"  window: [{args.since} .. {args.until}]  split={args.variant_split}  "
          f"(before='{args.variant_before}', after='{args.variant_after}')")
    print(f"  tier={args.tier}  sut_model={args.sut_model}  judge_model={args.judge_model} "
          f"(judged logs only)  require_eval_model={args.require_eval_model!r}")
    print(f"  gen_model: {'STAMP ' + gen_to_stamp if gen_to_stamp else 'OMITTED'} "
          f"(arm-specific: only the llm arm used an LLM; static arms are deterministic — "
          f"not per-log attributable)")
    print("-" * 92)
    print(f"  logs in window:            {counts['in_window']}")
    print(f"    by variant (mtime split): {dict(by_variant)}")
    print(f"  non-placeholder (skipped): {counts['non_placeholder']}  "
          f"(eval-model != {args.require_eval_model!r})")
    print(f"  unreadable (skipped):      {counts['unreadable']}")
    print(f"  WOULD PATCH:               {counts['would_patch']}  "
          f"(default={counts.get('would_patch_' + args.variant_before, 0)}, "
          f"hardened={counts.get('would_patch_' + args.variant_after, 0)})")
    print(f"    of which unjudged (no judge_model stamped): {counts['unjudged_no_judge_model']}")
    print(f"  skip (already provenance'd): {counts['skip_already_provenanced']}")
    print(f"  conflict (left untouched):   {counts['conflict_left_untouched']}")
    for bucket, label in [("patch", "would-patch"), ("skip", "already-provenanced"),
                          ("conflict", "CONFLICT"), ("non_placeholder", "non-placeholder"),
                          ("unjudged", "unjudged"), ("unreadable", "unreadable")]:
        if examples[bucket]:
            print(f"  e.g. {label}: {examples[bucket]}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Backfill additive provenance into existing study .eval logs (dry-run by default)."
    )
    ap.add_argument("--logs", nargs="+", default=["logs/*.eval"],
                    help="glob(s) of .eval logs to consider (mtime-window filtered).")
    ap.add_argument("--exclude", nargs="*", default=[], help="glob(s) to EXCLUDE.")
    ap.add_argument("--since", default=DEFAULT_SINCE, help="only logs with mtime >= this (ISO local / epoch).")
    ap.add_argument("--until", default=DEFAULT_UNTIL, help="only logs with mtime <= this (ISO local / epoch).")
    ap.add_argument("--variant-split", default=DEFAULT_SPLIT,
                    help="mtime cutoff: logs before -> --variant-before, at/after -> --variant-after.")
    ap.add_argument("--variant-before", default="default", help="variant name for logs before the split.")
    ap.add_argument("--variant-after", default="hardened", help="variant name for logs at/after the split.")
    ap.add_argument("--sut-model", default=DEFAULT_SUT_MODEL, help="real SUT model (certain, all arms).")
    ap.add_argument("--gen-model", default=DEFAULT_GEN_MODEL,
                    help="generation model value (NOT stamped unless --stamp-gen-model; arm-specific).")
    ap.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL,
                    help="judge model, stamped ONLY on logs with a real judge verdict.")
    ap.add_argument("--tier", default=DEFAULT_TIER, help="target tier to record (bridge for this study).")
    ap.add_argument("--require-eval-model", default="mockllm/model",
                    help="only patch logs whose core eval-model equals this placeholder (safety). "
                         "Set empty to disable the guard.")
    ap.add_argument("--stamp-gen-model", action="store_true",
                    help="ALSO write gen_model (study-level; NOT per-log arm-attributed) — off by default.")
    ap.add_argument("--max-logs", type=int, default=0, help="cap on logs considered (0 = no cap).")
    ap.add_argument("--progress-every", type=int, default=50, help="apply-progress print cadence.")
    ap.add_argument("--max-failures", type=int, default=10,
                    help="abort the apply batch after this many verification failures.")
    ap.add_argument("--apply", action="store_true", help="actually write (else dry run — no writes).")
    args = ap.parse_args()
    # Normalize an empty --require-eval-model to None (guard disabled).
    if not args.require_eval_model:
        args.require_eval_model = None
    run(args)


if __name__ == "__main__":
    main()
