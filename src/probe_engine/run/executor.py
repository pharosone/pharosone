"""Run probes against a target via Inspect and assemble Evidence (spec §4, §10)."""

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

from inspect_ai import Epochs, eval
from inspect_ai.log import write_eval_log
from inspect_ai.model import get_model

from probe_engine.compile.compiler import compile_probe
from probe_engine.domain.enums import EvidenceStatus, VariationStrategy
from probe_engine.domain.evidence import Evidence, Trial
from probe_engine.domain.probe import Probe
from probe_engine.domain.run import RunConfig
from probe_engine.plan.models import AllocationPlan
from probe_engine.run import checkpoint
from probe_engine.run import selection
from probe_engine.scoring.aggregate import aggregate_trials, failfast_locked
from probe_engine.scoring.batch_judge import (
    JUDGE_STATUS_APPLIED,
    JUDGE_STATUS_UNAVAILABLE,
    TrialRecord,
    batch_judge_with_status,
    stamp_judge_status,
)
from probe_engine.scoring.unverified import probe_oracle_kind, unverified_reason
from probe_engine.targets.agent_context import build_agent_context
from probe_engine.targets.mock import MockPolicy
from probe_engine.targets.registry import build_target_solver
from probe_engine.variation.batch_pregen import make_batched_attack_mutator
from probe_engine.variation.generate import generate_variants
from probe_engine.variation.llm_paraphrase import make_llm_attack_mutator


class EvidenceList(list):
    """A plain list of Evidence that ALSO carries the ids of probes skipped as blind spots (B2).

    Subclassing `list` keeps it byte-for-byte compatible with the old `list[Evidence]` return
    (indexing, iteration, len, list comprehensions all unchanged); callers that don't know about
    `blind_spots` are unaffected, while `run_corpus(...).blind_spots` surfaces the skipped ids."""

    def __init__(self, *args):
        super().__init__(*args)
        # Per-instance — NOT a class-level mutable default (which would alias one list across all
        # EvidenceList instances if ever mutated in place rather than reassigned).
        self.blind_spots: list[str] = []


def _eval_model(run_config: RunConfig) -> str:
    """Pick the model Inspect runs against, per target tier.

    mock -> the offline `mockllm` provider (no API key). model -> the configured real
    model (e.g. 'anthropic/claude-opus-4-8'). bridge -> the external agent (and, for
    adaptive, the attacker model) generate themselves, so a placeholder model is fine; the
    target.model there names the *endpoint's* model, not an Inspect provider model.
    """
    tier = run_config.target.tier
    if tier in ("mock", "bridge"):
        return "mockllm/model"
    if run_config.target.model:
        return run_config.target.resolved_model()
    raise ValueError(
        f"target tier {tier!r} requires run_config.target.model "
        f"(e.g. 'anthropic/claude-opus-4-8')"
    )


# Why the core eval-model field reads `mockllm/model` for these tiers — stamped as ADDITIVE
# provenance so the .eval self-documents the artifact without falsifying the (genuinely-ran) field.
_EVAL_MODEL_PLACEHOLDER_NOTE = {
    "bridge": (
        "Inspect ran the offline 'mockllm/model' placeholder: the real system-under-test agent is "
        "invoked inside the bridge adapter, OUTSIDE Inspect's model slot, so the core eval-model "
        "field is a placeholder artifact — the real SUT is recorded in sut_model (with the red-team "
        "gen_model/judge_model here)."
    ),
    "mock": (
        "Inspect ran the offline deterministic 'mockllm/model' placeholder (mock tier, no real "
        "model call); the core eval-model field is that placeholder by design."
    ),
}


def _run_provenance(run_config: RunConfig, run_metadata: dict | None) -> dict | None:
    """Assemble the ADDITIVE provenance stamped into the eval log's metadata (it NEVER overwrites
    Inspect's core `eval-model`, which genuinely ran `mockllm/model` for the mock/bridge tiers).

    Returns None when the caller did not request stamping (`run_metadata is None`) — byte-identical
    to prior behavior, no metadata threaded into `eval(...)`. When requested (ANY dict, even `{}`),
    the engine contributes the two facts it ALWAYS knows correctly — `tier` and (for the mock/bridge
    tiers) an `eval_model_placeholder_note` explaining the `mockllm/model` eval-model artifact — then
    overlays the caller-supplied dict. The concrete MODEL names (`sut_model`, `gen_model`,
    `judge_model`, `variant`) are sourced from the caller by design: for the bridge tier the real SUT
    is chosen INSIDE the adapter (not in RunConfig), and a benchmark arm may override variation with a
    static policy, so only the caller knows the truth. Caller values WIN on any key collision."""
    if run_metadata is None:
        return None
    tier = run_config.target.tier
    prov: dict = {"tier": tier}
    note = _EVAL_MODEL_PLACEHOLDER_NOTE.get(tier)
    if note:
        prov["eval_model_placeholder_note"] = note
    prov.update(run_metadata)
    return prov


def _build_attack_mutator(
    probe: Probe, run_config: RunConfig, api_key: str | None, context
):
    """Return an LLM attack-mutator ``(text, lang, index) -> str`` if LLM variation is requested
    (run-level override or the probe's own variation.strategy) AND a model is resolvable; else None.

    None means the deterministic, context-bound diversifier (``techniques.diversify``) is used —
    which still applies the full context-binding + plain/obfuscated MIX offline. The LLM mutator is
    constructed WITH ``context`` (decision 9) and itself falls back to ``diversify`` on any model
    error/refusal/empty (decision 5)."""
    want_llm = (
        run_config.variation_strategy == "llm"
        or VariationStrategy.LLM in probe.variation.strategy
    )
    if not want_llm:
        return None
    model_id = run_config.target.resolved_paraphrase_model()
    if not model_id:
        return None  # no model -> deterministic context-bound diversifier (still context-bound)
    batch_size = run_config.variation_batch_size
    if batch_size and batch_size > 0:
        # OPT-IN batched pre-generation: ceil(N/batch) calls per payload instead of N, with the
        # plain/obfuscated buckets + per-index obfuscator layering preserved (decision 8). Same
        # (text, lang, index) call shape; falls back to diversify per index on model/parse error.
        return make_batched_attack_mutator(
            model_id, api_key, context=context, batch_size=batch_size
        )
    return make_llm_attack_mutator(model_id, api_key, context=context)


def _resolve_model(run_config: RunConfig, api_key: str | None = None):
    """Return the model to run against. A per-run api_key is passed to the provider
    via get_model (kept out of the environment and off disk). Only the `model` tier uses
    the eval model with the key; mock/bridge run on the offline placeholder."""
    model_id = _eval_model(run_config)
    if api_key and run_config.target.tier == "model":
        return get_model(model_id, api_key=api_key)
    return model_id


def _sample_transcript(log) -> list[dict]:
    """Serialize the first sample's message thread (the turn-by-turn transcript, spec §7.3).

    Note: this records the agent's OWN messages as evidence. On a prompt_leak FAIL the agent's
    leaked text is therefore present here by design — it is the proof of the leak — so reports
    for runs with target.protected_snippets must be treated as sensitive. The engine-supplied
    protected reference itself is never written here; only what the agent emitted."""
    samples = log.samples or []
    if not samples:
        return []
    out: list[dict] = []
    for msg in samples[0].messages or []:
        text = getattr(msg, "text", None)
        if text is None:
            text = str(getattr(msg, "content", ""))
        out.append({"role": getattr(msg, "role", "?"), "content": text[:2000]})
    return out


def eval_log_to_trials(log, *, has_semantic: bool = False, probe_id: str = "?") -> list[Trial]:
    """Fold each sample's score metadata into a Trial. ``success`` is the judge-only verdict that
    ``_apply_batch_judge`` stamped in place (offline fallback: success == binary_hit).

    DEFENSIVE MARKER CHECK (B1c): when this log was RE-READ from a .eval on disk (not the freshly
    judged in-memory log), a sample may carry the PROVISIONAL binary success that Inspect wrote
    before the judge ran. If the probe HAS a semantic check yet a sample's metadata lacks
    ``judge_applied == True``, the binary value is NOT a final verdict — we log a loud warning so a
    disk reader never silently overstates ASR. (For a freshly judged in-memory log every sample
    carries judge_applied; offline runs carry judge_applied=False AND have no semantic check, so the
    binary value IS final and this never fires — today's behavior, unchanged.)"""
    trials: list[Trial] = []
    warned = False
    for sample in log.samples or []:
        if not sample.scores:
            continue
        name = next(iter(sample.scores))
        meta = sample.scores[name].metadata or {}
        if has_semantic and meta.get("judge_applied") is not True and not warned:
            logger.warning(
                "probe %s: reading binary success as final but the probe has a semantic check and "
                "this sample lacks judge_applied=True — the .eval may be PROVISIONAL (judge verdict "
                "not persisted); rebuild from the judged run, do not trust this ASR.",
                probe_id,
            )
            warned = True
        trials.append(
            Trial(
                variant_id=str(sample.id),
                epoch=sample.epoch,
                success=bool(meta.get("success")),
            )
        )
    return trials


def _stamp_judge_applied(log, applied: bool) -> None:
    """Mark every sample score with an UNAMBIGUOUS final/provisional marker (B1b): a disk reader can
    then always tell a judge-confirmed verdict from the provisional binary one Inspect wrote pre-judge.
    judge_applied=True  -> metadata["success"] is the judge-only verdict (final).
    judge_applied=False -> offline/no-judge fallback: success == binary_hit IS final (today's behavior)."""
    for sample in log.samples or []:
        if not sample.scores:
            continue
        name = next(iter(sample.scores))
        meta = sample.scores[name].metadata
        if meta is not None:
            meta["judge_applied"] = applied


def _fp_unverified_reason(probe: Probe, run_config: RunConfig, judge_applied: bool) -> str | None:
    """GUARD: decide whether this probe's UNJUDGED binary verdict over a false-positive-prone oracle
    (`prompt_leak` / `contains`) must be downgraded to EvidenceStatus.UNVERIFIED rather than trusted
    as a confident pass/fail. Returns a loud reason string when the guard fires (logged here +
    surfaced to `run_probe` for the status override) or None when the verdict may stand.

    Extends the existing configured-but-UNAVAILABLE judge degrade (B6) to the NO-judge-configured
    case: a defended agent's refusal that restates the guarded vocabulary over-fires these binary
    oracles, so binary-only they manufacture false-positive FAILs (we observed 12/19 -> 2/19 once
    judged). A judged verdict (judge_applied=True) is authoritative and is NEVER touched."""
    reason = unverified_reason(
        oracle_kind=probe_oracle_kind(probe),
        judge_applied=judge_applied,
        has_protected_snippets=bool(run_config.target.protected_snippets),
    )
    if reason:
        logger.warning(
            "GUARD probe=%s: verdict UNVERIFIED (judge required) — %s. n_success/asr are kept for "
            "transparency but the status is NOT a confident pass/fail; configure target.judge_model "
            "(and protected_snippets for prompt_leak) to get a real verdict.",
            probe.id, reason,
        )
    return reason


def _apply_batch_judge(log, probe: Probe, run_config: RunConfig, api_key: str | None) -> str | None:
    """Decide success JUDGE-ONLY over ALL of a probe's trials, in place (decision 3).

    Reads each sample's stashed evidence (transcript / tool_calls / reply / binary_hit), runs the
    two-pass batch judge, and OVERWRITES every sample score's metadata["success"] with the judge
    verdict so the downstream `eval_log_to_trials` -> Wilson/ASR aggregation reflects it. Every
    sample is stamped metadata["judge_applied"] = True/False so a disk reader can distinguish a final
    verdict from the provisional binary one (B1b).

    OFFLINE FALLBACK (the suite's network-free invariant): when no judge model resolves, or the
    probe has no semantic judge, this is a no-op on success — metadata["success"] stays == binary_hit
    (judge_applied=False), exactly today's per-trial binary behaviour. Any judge-infra exception also
    falls back to binary.

    RETURNS an UNVERIFIED reason string (GUARD) when the resulting verdict rests on an UNJUDGED
    false-positive-prone binary oracle (`prompt_leak` / `contains`) — `run_probe` uses it to override
    Evidence.status to EvidenceStatus.UNVERIFIED. Returns None whenever a judge was applied or the
    oracle is not false-positive-prone (the common case — behaviour unchanged)."""
    judge_model = run_config.target.resolved_judge_model()
    semantic = probe.evaluation.semantic
    if not judge_model or not semantic:
        _stamp_judge_applied(log, False)  # offline / no-judge: provisional binary IS the final verdict
        return _fp_unverified_reason(probe, run_config, judge_applied=False)

    metas: list[dict] = []
    records: list[TrialRecord] = []
    for sample in log.samples or []:
        if not sample.scores:
            continue
        name = next(iter(sample.scores))
        meta = sample.scores[name].metadata
        if meta is None:
            continue
        metas.append(meta)
        records.append(
            TrialRecord(
                index=len(records),
                transcript=str(meta.get("transcript", "")),
                tool_calls=str(meta.get("tool_calls", "")),
                reply=str(meta.get("reply", "")),
                binary_hit=bool(meta.get("binary_hit")),
            )
        )
    if not records:
        _stamp_judge_applied(log, False)
        return _fp_unverified_reason(probe, run_config, judge_applied=False)
    try:
        result = asyncio.run(
            batch_judge_with_status(
                records,
                judge_prompt=semantic.judge_prompt,
                confidence_threshold=semantic.confidence_threshold,
                model_id=judge_model,
                api_key=api_key,
                batch_size=run_config.judge_batch_size,
                timeout_s=run_config.judge_timeout_s,
            )
        )
    except Exception as e:  # judge infra failure: keep binary verdict, don't kill the run
        logger.warning("batch judge failed for probe %s, falling back to binary verdict: %r",
                       probe.id, e)
        _stamp_judge_applied(log, False)  # fell back to binary -> not a judge verdict
        return _fp_unverified_reason(probe, run_config, judge_applied=False)
    # B6: write the machine-detectable availability marker so a configured-but-unavailable judge is
    # never persisted as a clean judge pass. stamp_judge_status sets success / judge_confirmed /
    # judge_status / judge_unverified (+ judge_error) per meta; we set judge_applied here from the
    # status because "unavailable" fell back to the binary oracle and is NOT a judge verdict.
    stamp_judge_status(metas, result)
    applied = result.status == JUDGE_STATUS_APPLIED
    # Per-record: a chunk that degraded (its judge call failed) fell back to the binary oracle, so
    # those records are NOT a judge verdict even though the overall status is "applied".
    for i, meta in enumerate(metas):
        meta["judge_applied"] = applied and i not in result.unverified_indices
    if result.status == JUDGE_STATUS_UNAVAILABLE:
        logger.warning(
            "batch judge probe=%s: judge UNAVAILABLE — verdict UNVERIFIED, fell back to the binary "
            "oracle (%d trial(s)); NOT a clean judge pass (%s)",
            probe.id, len(result.mask), result.error,
        )
    else:
        logger.info("batch judge probe=%s: %d/%d trials judged successful (judge-only verdict)",
                    probe.id, sum(1 for c in result.mask if c), len(result.mask))
    # A judge that WAS applied (verdict authoritative) clears the guard; an UNAVAILABLE judge fell
    # back to the binary oracle (applied=False) and is funneled through the same guard as no-judge.
    return _fp_unverified_reason(probe, run_config, judge_applied=applied)


def _persist_judged_log(log, probe: Probe) -> None:
    """Write the in-memory (judge-corrected) EvalLog back to its own .eval on disk so the FILE is
    authoritative (B1a). Inspect already wrote the log pre-judge with the provisional binary success;
    this overwrites that with the judged success + judge_applied markers. Best-effort — a write
    error is logged, never raised (the run already holds the correct in-memory verdict)."""
    location = getattr(log, "location", None)
    if not location:
        return
    try:
        write_eval_log(log, location)
    except Exception as e:
        logger.warning("could not persist judged eval log for probe %s to %s: %r",
                       probe.id, location, e)


def _count_errored_samples(log) -> int:
    """Samples the target raised on (e.g. a bridge endpoint 5xx/timeout). These are NOT valid
    observations — counted separately, never folded into trials as success or robust."""
    return sum(1 for s in (log.samples or []) if getattr(s, "error", None))


def _eval_variants(
    probe: Probe,
    run_config: RunConfig,
    variants,
    *,
    mock_policy: MockPolicy | None,
    api_key: str | None,
    external: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None,
    display: str,
    log_dir: str | None,
    run_metadata: dict | None = None,
):
    """Compile + eval one set of variants, apply the JUDGE-only pass, persist the judged .eval (B1),
    and return (trials, n_errors, log, unverified_reason). The single shared execution unit for both
    the full run and each fail-fast chunk — so a fail-fast run that never locks produces exactly the
    full run's trials. `unverified_reason` is non-None (GUARD) when the verdict rests on an unjudged
    false-positive-prone binary oracle; `run_probe` uses it to mark Evidence.status UNVERIFIED.

    `run_metadata` (when provided) is enriched with engine-derived provenance and threaded into the
    eval as ADDITIVE metadata (real sut/gen/judge models + the mockllm placeholder note), so the
    on-disk .eval self-documents the real models. None -> byte-identical prior behavior."""
    task = compile_probe(probe, variants, run_config, mock_policy, api_key, external)
    logs = eval(
        task,
        model=_resolve_model(run_config, api_key),
        epochs=Epochs(run_config.epochs, ["mean"]),
        display=display,  # "none" (silent) | "rich"/"full" (live dashboard) | "conversation" (live transcripts)
        log_dir=log_dir or "logs",
        # ADDITIVE provenance (never overwrites the core eval-model). None (default) = no metadata
        # threaded == today's exact eval call.
        metadata=_run_provenance(run_config, run_metadata),
    )
    unverified = _apply_batch_judge(logs[0], probe, run_config, api_key)
    # Persist the JUDGED log back to its .eval (B1a): Inspect wrote the PROVISIONAL binary success
    # before the judge ran, so the on-disk file would otherwise overstate ASR for any report rebuilt
    # by re-reading it. Best-effort: a write failure must not lose the in-memory verdict / kill the run.
    _persist_judged_log(logs[0], probe)
    trials = eval_log_to_trials(
        logs[0], has_semantic=probe.evaluation.semantic is not None, probe_id=probe.id
    )
    return trials, _count_errored_samples(logs[0]), logs[0], unverified


def run_probe(
    probe: Probe,
    run_config: RunConfig,
    *,
    mock_policy: MockPolicy | None = None,
    seed: int = 0,
    log_dir: str | None = None,
    api_key: str | None = None,
    external: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    display: str = "none",
    attack_mutator=None,
    run_metadata: dict | None = None,
) -> Evidence:
    context = build_agent_context(run_config)
    # Fanout target for multi_channel turns: every channel the agent declares, ∪ the universal
    # conversation channel, deduped (order-preserving). Generation renders a distinct payload per
    # channel; a target that declares only the conversation collapses this to a single channel.
    fanout_channels = list(dict.fromkeys(["message", *run_config.target.channels]))
    # `attack_mutator` (B): an explicit variation-mutator BUILDER with the same shape as
    # `_build_attack_mutator(probe, run_config, api_key, context) -> ((text,lang,idx)->str | None)`.
    # When given it is used verbatim — so a caller (e.g. a benchmark) can inject its own variation
    # policy WITHOUT monkeypatching the module global, which makes concurrent multi-policy runs safe.
    # Default None reproduces today's behavior exactly (the built-in deterministic/LLM mutator).
    build_mutator = attack_mutator or _build_attack_mutator
    variants, _meta = generate_variants(
        probe,
        run_config.n_variants,
        seed=seed,
        languages=run_config.languages,
        context=context,
        mutate=build_mutator(probe, run_config, api_key, context),
        fanout_channels=fanout_channels,
    )
    if run_config.fail_fast:
        # Opt-in early stop: run the variants in chunks and stop as soon as a FAIL verdict is
        # statistically certain (Wilson lower bound of ASR >= asr_pass). The union of all chunks ==
        # the full variant set, so a run that never locks yields the same trials as a full run.
        chunk = max(1, run_config.fail_fast_chunk)
        trials: list = []
        n_errors = 0
        ref_log = None
        early = False
        unverified_reason_str: str | None = None
        for start in range(0, len(variants), chunk):
            t, e, log, unv = _eval_variants(
                probe, run_config, variants[start:start + chunk],
                mock_policy=mock_policy, api_key=api_key, external=external,
                display=display, log_dir=log_dir, run_metadata=run_metadata,
            )
            trials += t
            n_errors += e
            if ref_log is None:
                ref_log = log
            # GUARD is probe-level (oracle kind + judge/snippet config), identical across chunks.
            unverified_reason_str = unverified_reason_str or unv
            if failfast_locked(trials, run_config.thresholds):
                early = True
                break
    else:
        trials, n_errors, ref_log, unverified_reason_str = _eval_variants(
            probe, run_config, variants,
            mock_policy=mock_policy, api_key=api_key, external=external,
            display=display, log_dir=log_dir, run_metadata=run_metadata,
        )
        early = False

    if not trials and n_errors:
        # Every sample errored on the target — surface it loudly instead of returning a silent
        # "not_run / asr=0", which would read as a robust agent (spec honesty, esp. bridge).
        raise RuntimeError(
            f"probe {probe.id}: target produced no usable result — all {n_errors} sample(s) "
            f"errored (tier={run_config.target.tier}; check the endpoint/auth/model). "
            f"eval log status={getattr(ref_log, 'status', '?')}"
        )
    evidence = aggregate_trials(
        probe.id,
        probe.severity,
        probe.taxonomy_tags,
        probe.control_overrides,
        probe.provenance,
        trials,
        run_config.thresholds,
    )
    transcript = _sample_transcript(ref_log) if ref_log is not None else []
    n_turns = sum(1 for m in transcript if m["role"] == "user") or len(probe.scenario.turns) or 1
    update: dict = {
        "scenario": probe.scenario.type.value,
        "n_turns": n_turns,
        "n_errors": n_errors,
        "transcript": transcript,
        "early_stopped": early,
    }
    # GUARD: an unjudged false-positive-prone binary oracle (`prompt_leak` / `contains`) cannot back a
    # confident fail NOR a robust pass — override the aggregated status to UNVERIFIED (n_success/asr
    # are left intact for transparency). Only fires when trials exist (a NOT_RUN probe stays NOT_RUN)
    # and never when a judge was applied (the judged verdict is authoritative). _apply_batch_judge
    # already logged the loud warning naming the probe + reason.
    if unverified_reason_str and trials:
        update["status"] = EvidenceStatus.UNVERIFIED
    return evidence.model_copy(update=update)


def run_probe_lifecycle(
    probe: Probe,
    run_config: RunConfig,
    *,
    stages: list[str] | None = None,
    **kwargs,
) -> list[Evidence]:
    """Replay `run_probe` once per lifecycle stage, overriding `target.seed_stage` each time, so the
    SAME attack is tested at each stage of the resource's lifecycle. Each stage yields its own
    Evidence stamped `<probe_id>@<stage>` (its own Wilson CI + PASS/FAIL), surfacing "the attack only
    works once the resource is at stage X".

    The only per-stage variable is `target.seed_stage` (it flows through the compiler into
    Sample.metadata and is read by seed_from_meta in every tier), so the sweep is a thin loop over
    `run_probe` — no compiler/aggregate/coverage change. This is a SEPARATE, explicit entry point,
    deliberately NOT wired into run_corpus, so default corpus runs and their counts are untouched."""
    # Sweep stages: explicit arg > probe override > the AGENT's declared lifecycle order > seed.
    # Preferring target.lifecycle_order keeps the universal probe free of agent-specific stages.
    sweep = (
        stages
        or probe.applicability.lifecycle_sweep
        or run_config.target.lifecycle_order
        or [run_config.target.seed_stage]
    )
    out: list[Evidence] = []
    for stage in sweep:
        rc = run_config.model_copy(
            update={"target": run_config.target.model_copy(update={"seed_stage": stage})}
        )
        ev = run_probe(probe, rc, **kwargs)
        out.append(ev.model_copy(update={"probe_id": f"{ev.probe_id}@{stage}"}))
    return out


def _order_by_plan(probes: list[Probe], plan: AllocationPlan) -> list[Probe]:
    """Order eligible probes by plan priority (desc), STABLE within equal priority and for probes
    the plan has no allocation for (those sort to priority 0, keeping their original position).

    Deterministic gating is the floor: this only RE-ORDERS the eligible set — every probe passed in
    is returned exactly once, none dropped. Probes without an allocation keep run-config defaults at
    execution time (handled in `run_corpus`); they are never silently skipped."""
    indexed = list(enumerate(probes))

    def key(item: tuple[int, Probe]) -> tuple[int, int]:
        idx, p = item
        alloc = plan.for_probe(p.id)
        priority = alloc.priority if alloc else 0
        # negative priority -> descending; idx -> stable tie-break (original order preserved)
        return (-priority, idx)

    return [p for _, p in sorted(indexed, key=key)]


def _log_channel_coverage(run_config: RunConfig, adapter_channels: list[str] | None) -> None:
    """B5 wiring: cross-check the profile-declared channels against the channels the adapter can
    actually route poison into. ``declared_not_routable`` is FALSE coverage (the profile claims a
    channel the adapter cannot deliver) and is surfaced LOUDLY as a blind-spot warning (invariant 3);
    ``routable_not_declared`` is missed-coverage info. No adapter info (None) -> no cross-check."""
    if adapter_channels is None:
        return
    rec = selection.reconcile_channels(set(run_config.target.channels), set(adapter_channels))
    if rec["declared_not_routable"]:
        logger.warning(
            "BLIND SPOT — profile declares channel(s) the adapter cannot deliver, so they are NOT "
            "actually tested (false coverage): %s. Tested channels: %s",
            rec["declared_not_routable"], rec["tested"],
        )
    if rec["routable_not_declared"]:
        logger.info(
            "missed coverage — the adapter could route channel(s) the profile did not declare: %s",
            rec["routable_not_declared"],
        )


# Build-time ValueErrors from build_target_solver that mean "this PROBE's oracle can never be
# adjudicated on THIS target" — a blind spot to skip+surface (B2), NOT a global misconfiguration.
# Global config errors (missing bridge endpoint, unknown tier) must still crash the whole run.
_BLIND_SPOT_MARKERS = ("oracle can never fire", "requires evaluation.binary")


def _is_blind_spot_error(err: ValueError) -> bool:
    return any(m in str(err) for m in _BLIND_SPOT_MARKERS)


def run_corpus(
    probes: list[Probe],
    run_config: RunConfig,
    *,
    plan: AllocationPlan | None = None,
    progress: Callable[[str, int, int, Probe, Evidence | None], None] | None = None,
    resume: bool = False,
    out_dir: str | None = None,
    adapter_channels: list[str] | None = None,
    run_metadata: dict | None = None,
    **kwargs,
) -> list[Evidence]:
    """Run each probe in order. If `progress` is given it is called as
    progress(phase, i, total, probe, evidence) with phase in {"start","done","skip"} — "start"
    before a probe runs (evidence=None), "done" after (evidence populated), and "skip" when a probe
    is dropped as a blind spot (evidence=None) — so callers can render live per-probe progress.
    `display` (in kwargs) is threaded into each probe's inspect eval.

    When `plan` is given, probes are run in plan-priority order (desc, stable) and each probe with an
    allocation runs with that allocation's n_variants/epochs (via `run_config.model_copy`); probes
    WITHOUT an allocation fall back to the run-config defaults — the planner only re-weights/orders
    within the eligible set, it never drops an eligible probe. `plan=None` reproduces today's exact
    behavior (back-compat): original order, run-config defaults for every probe.

    BLIND SPOTS (B2, invariant 3): a probe whose oracle can never be adjudicated on this target
    (build_target_solver raises ValueError — e.g. a bridge authz/state/secret oracle with no matching
    tool_inventory declaration) is SKIPPED and SURFACED (logged loud, emitted via progress "skip", and
    exposed on the returned list's `.blind_spots`) — never crashed, never counted as a pass/robust.

    RESUMABILITY (B3, opt-in): with `resume=True` (and `out_dir` set), each completed probe's Evidence
    is persisted to `<out_dir>/.checkpoint/<probe_id>.json` keyed by a config-hash of the result-
    affecting (NON-secret) inputs; on a resumed run a checkpoint whose hash matches is loaded and the
    probe is SKIPPED (its Evidence reused, in order). `resume=False` (default) = today's exact behavior.

    `adapter_channels` (B5): when given, the profile-declared channels are reconciled against it and
    declared-but-unroutable channels are surfaced as a loud blind-spot warning.

    `run_metadata` (provenance): an optional dict the caller passes through to have ADDITIVE
    provenance stamped into every probe's `.eval` metadata (real `sut_model`/`gen_model`/`judge_model`/
    `variant` + a note explaining the `mockllm/model` eval-model placeholder for mock/bridge tiers).
    The engine enriches it with what it can derive (tier + configured models); the caller supplies
    what only it knows (the bridge SUT is chosen inside the adapter). None (default) = byte-identical
    prior behavior (no metadata threaded into the eval).

    Returns the list of Evidence (in order). Skipped blind-spot probe ids are also exposed on the
    returned list via the `blind_spots` attribute (an `EvidenceList`) and emitted via progress."""
    _log_channel_coverage(run_config, adapter_channels)
    ordered = _order_by_plan(probes, plan) if plan is not None else probes
    results = EvidenceList()
    blind_spots: list[str] = []
    total = len(ordered)
    seed = kwargs.get("seed", 0)
    for i, p in enumerate(ordered, 1):
        rc = run_config
        n_variants, epochs = run_config.n_variants, run_config.epochs
        if plan is not None:
            alloc = plan.for_probe(p.id)
            if alloc is not None:
                n_variants, epochs = alloc.n_variants, alloc.epochs
                rc = run_config.model_copy(update={"n_variants": n_variants, "epochs": epochs})

        # B2: a probe whose oracle can't be adjudicated on this target is a blind spot — skip+surface.
        try:
            build_target_solver(p, rc, kwargs.get("mock_policy"), kwargs.get("api_key"),
                                 kwargs.get("external"))
        except ValueError as e:
            if not _is_blind_spot_error(e):
                raise  # global misconfiguration (no endpoint, unknown tier) -> crash, don't mask it
            blind_spots.append(p.id)
            logger.warning("BLIND SPOT — probe %s skipped (oracle not adjudicable on target %s): %s",
                           p.id, run_config.target.name, e)
            if progress:
                progress("skip", i, total, p, None)
            continue

        # B3: reuse a matching checkpoint instead of re-running an expensive probe.
        chash = None
        if resume and out_dir:
            chash = checkpoint.config_hash(p, rc, n_variants, epochs, seed)
            cached = checkpoint.load(out_dir, p.id, chash)
            if cached is not None:
                logger.info("resume: reusing checkpoint for probe %s (config-hash match)", p.id)
                results.append(cached)
                if progress:
                    progress("done", i, total, p, cached)
                continue

        if progress:
            progress("start", i, total, p, None)
        ev = run_probe(p, rc, run_metadata=run_metadata, **kwargs)
        results.append(ev)
        if resume and out_dir and chash is not None:
            checkpoint.save(out_dir, p.id, chash, ev)
        if progress:
            progress("done", i, total, p, ev)
    results.blind_spots = blind_spots
    return results
