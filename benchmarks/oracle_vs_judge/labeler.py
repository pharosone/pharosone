"""Part B — independent reference labeling (replaces human ground truth).

A stratified sample of trials (oversampling oracle/judge DISAGREEMENTS and positives — that is where
the two signals fight and where the truth matters) is labeled by an INDEPENDENT strong model, reusing
the engine's OWN judge logic (`scoring.judge.judge_confirms`) with the model id swapped. The label is
"did the agent actually perform the unsafe behavior in this transcript?" — exactly the judge's task.

INDEPENDENCE IS LOAD-BEARING: the labeler model MUST be a different family from BOTH the LLM judge
(`z-ai/glm-5.2`) and the SUT (`deepseek/*`), or self-family leniency would contaminate the reference.
`independence_blocker` enforces this; keyed labeling is refused on a collision.

Keyed labeling is GATED elsewhere (the CLI): this module's `label_sample` only runs when a key is
present and the caller opted in. The sampling + cost-estimate functions are pure / offline.
"""

from __future__ import annotations

import asyncio
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass

from probe_engine.corpus.loader import load_corpus
from probe_engine.scoring.judge import judge_confirms

from benchmarks.oracle_vs_judge.extract import TrialRow

# The judge's per-trial template truncates transcript/tools/reply to these char caps (see
# `scoring.judge._TEMPLATE` usage); the cost estimate mirrors them so it reflects the real prompt.
_JUDGE_TRANSCRIPT_CAP = 6000
_JUDGE_TOOLS_CAP = 3000
_JUDGE_REPLY_CAP = 2000
_CHARS_PER_TOKEN = 4.0          # rough OpenRouter token heuristic for the offline estimate
_TEMPLATE_OVERHEAD_TOK = 400    # the judge prompt scaffolding around the transcript/tools/reply
_LABEL_OUT_TOK = 90             # one JSON verdict per trial

# Per-1M-token USD prices for a few currently-valid OpenRouter slugs (fetched from the live models
# list, Jun 2026). Used only for the offline cost estimate; override via CLI for any other slug.
PRICES: dict[str, tuple[float, float]] = {
    "openrouter/anthropic/claude-opus-4.8": (5.00, 25.00),
    "anthropic/claude-opus-4.8": (5.00, 25.00),
    "openrouter/anthropic/claude-sonnet-4.6": (3.00, 15.00),
    "anthropic/claude-sonnet-4.6": (3.00, 15.00),
    "openrouter/openai/gpt-5.2": (1.75, 14.00),
    "openai/gpt-5.2": (1.75, 14.00),
}


def model_family(slug: str) -> str:
    """The provider ORG token of an OpenRouter slug, stripping a leading router prefix.

    `openrouter/anthropic/claude-opus-4.8` -> `anthropic`; `z-ai/glm-5.2` -> `z-ai`;
    `deepseek/deepseek-v4-flash` -> `deepseek`. Empty/unknown -> ''."""
    s = (slug or "").strip().lower()
    if s.startswith("openrouter/"):
        s = s[len("openrouter/"):]
    return s.split("/", 1)[0] if s else ""


def independence_blocker(labeler_model: str, judge_model: str, sut_model: str) -> str | None:
    """Return a human-readable blocker iff the labeler shares a family with the judge OR the SUT.

    Independence is what lets the labels stand in for ground truth; a same-family labeler would just
    re-encode the judge's (or the SUT's) own biases. Returns None when the labeler is independent."""
    lf = model_family(labeler_model)
    jf = model_family(judge_model)
    sf = model_family(sut_model)
    if not lf:
        return f"labeler model {labeler_model!r} has no resolvable family"
    collisions = [name for name, fam in (("judge", jf), ("SUT", sf)) if fam and fam == lf]
    if collisions:
        return (
            f"labeler family {lf!r} collides with the {' & '.join(collisions)} "
            f"(judge={jf!r}, sut={sf!r}) — pick a different family (e.g. anthropic/* or openai/*) "
            f"so self-family bias cannot contaminate the reference labels"
        )
    return None


# ----------------------------------------------------------------------------------------------
# Stratified sampling (PURE, deterministic, unit-tested).
# ----------------------------------------------------------------------------------------------
# Target share of the cap per stratum, in PRIORITY order. Disagreements get the largest slice
# (they are usually a small minority of trials, so 0.5 is a heavy OVERSAMPLE), positives next, clean
# negatives last. Any slice a stratum can't fill is redistributed to the others by the same priority,
# so the cap is always used to the extent the pool allows. The buckets ARE the strata, so a sample
# built this way is stratified AND oversamples disagreements + positives, as the spec requires.
DEFAULT_STRATUM_SHARES: tuple[float, float, float] = (0.5, 0.3, 0.2)

_BUCKET_ORDER = ("disagreement", "positive_agreement", "negative_agreement")


@dataclass(frozen=True)
class SampleComposition:
    """Counts describing a stratified reference sample (for the report + the dry-run plan)."""

    pool: int                 # trials eligible for sampling (after the judge_applied filter)
    disagreements: int        # taken from the disagreement bucket
    positive_agreements: int  # taken from the both-hit bucket
    negative_agreements: int  # taken from the both-no bucket
    total: int                # disagreements + positive_agreements + negative_agreements
    pool_disagreements: int   # bucket population (before capping)
    pool_positive_agreements: int
    pool_negative_agreements: int

    def as_dict(self) -> dict:
        return {
            "pool": self.pool,
            "total": self.total,
            "taken": {
                "disagreements": self.disagreements,
                "positive_agreements": self.positive_agreements,
                "negative_agreements": self.negative_agreements,
            },
            "pool_buckets": {
                "disagreements": self.pool_disagreements,
                "positive_agreements": self.pool_positive_agreements,
                "negative_agreements": self.pool_negative_agreements,
            },
        }


def _bucket(row: TrialRow) -> str:
    """Classify a trial into one of the three sampling buckets."""
    if row.binary_hit != row.judge_success:
        return "disagreement"
    return "positive_agreement" if row.binary_hit else "negative_agreement"


def _allocate(cap: int, avail: dict[str, int], shares: tuple[float, float, float]) -> dict[str, int]:
    """Allocate `cap` slots across the three strata by `shares` (floor), then redistribute the
    leftover greedily in priority order to any stratum that still has capacity. Pure arithmetic.

    Guarantees: each stratum gets at most its availability; disagreements (then positives) absorb
    any slack first, so a sparse positives/negatives pool never wastes budget."""
    alloc = {name: min(avail[name], int(math.floor(share * cap)))
             for name, share in zip(_BUCKET_ORDER, shares)}
    remaining = cap - sum(alloc.values())
    for name in _BUCKET_ORDER:
        if remaining <= 0:
            break
        room = avail[name] - alloc[name]
        take = min(remaining, room)
        alloc[name] += take
        remaining -= take
    return alloc


def select_reference_sample(
    rows: Sequence[TrialRow], *, cap: int, seed: int, judge_applied_only: bool = True,
    shares: tuple[float, float, float] = DEFAULT_STRATUM_SHARES,
) -> tuple[list[TrialRow], SampleComposition]:
    """Pick a STRATIFIED <=cap sample that OVERSAMPLES disagreements and positives.

    The three strata are DISAGREEMENTS (oracle != judge — the contested trials), POSITIVE_AGREEMENTS
    (both flagged a hit), and NEGATIVE_AGREEMENTS (both clean). `cap` is split by `shares` (default
    50/30/20) and any unfillable slice is redistributed by priority (disagreements first), so the
    contested + positive trials are heavily over-represented relative to their natural frequency
    while clean trials still appear (a genuinely stratified sample, not a disagreements-only slice).

    Deterministic: within a stratum that overflows its allocation, a `random.Random(seed)` sample is
    drawn from the stably-sorted bucket, so the same (rows, cap, seed) always yields the same sample.

    When `judge_applied_only` (default), only trials with a REAL judge verdict are eligible — those
    are the only ones where the oracle and the judge can actually disagree, and the only ones whose
    `judge_success` is a genuine judge prediction worth scoring against the reference."""
    pool = [r for r in rows if (r.judge_applied or not judge_applied_only)]
    buckets: dict[str, list[TrialRow]] = {name: [] for name in _BUCKET_ORDER}
    for r in pool:
        buckets[_bucket(r)].append(r)

    avail = {name: len(buckets[name]) for name in _BUCKET_ORDER}
    alloc = _allocate(cap, avail, shares)

    rng = random.Random(seed)
    picked: list[TrialRow] = []
    for name in _BUCKET_ORDER:
        bucket = sorted(buckets[name], key=lambda r: r.key())
        want = alloc[name]
        if len(bucket) > want:
            bucket = rng.sample(bucket, want)
            bucket.sort(key=lambda r: r.key())
        picked.extend(bucket)

    composition = SampleComposition(
        pool=len(pool),
        disagreements=alloc["disagreement"],
        positive_agreements=alloc["positive_agreement"],
        negative_agreements=alloc["negative_agreement"],
        total=len(picked),
        pool_disagreements=avail["disagreement"],
        pool_positive_agreements=avail["positive_agreement"],
        pool_negative_agreements=avail["negative_agreement"],
    )
    return picked, composition


# ----------------------------------------------------------------------------------------------
# Semantic index + cost estimate (offline).
# ----------------------------------------------------------------------------------------------
def semantic_index(corpus_dir: str) -> dict[str, dict]:
    """Map probe_id -> {question, threshold} for every probe with a semantic check.

    The labeler asks the SAME unsafe-behavior question the engine's judge asks, so it needs the
    probe's `semantic.judge_prompt`. Probes without a semantic check never reach a real judge
    (judge_applied is always False), so they carry no reference question."""
    index: dict[str, dict] = {}
    for probe in load_corpus(corpus_dir):
        sem = probe.evaluation.semantic
        if sem is None:
            continue
        index[probe.id] = {
            "question": sem.judge_prompt,
            "threshold": float(sem.confidence_threshold),
        }
    return index


def _approx_tokens(row: TrialRow) -> int:
    """Approximate the labeler prompt tokens for one trial (the judge template + capped fields)."""
    chars = (
        min(len(row.transcript), _JUDGE_TRANSCRIPT_CAP)
        + min(len(row.tool_calls), _JUDGE_TOOLS_CAP)
        + min(len(row.reply), _JUDGE_REPLY_CAP)
    )
    return int(chars / _CHARS_PER_TOKEN) + _TEMPLATE_OVERHEAD_TOK


def estimate_label_cost(
    sample_rows: Sequence[TrialRow], model_id: str,
    *, price_in: float | None = None, price_out: float | None = None,
) -> dict:
    """Project the labeling spend: ONE `judge_confirms` call per trial (no batching — per-trial
    precision matters for the reference). Tokens are summed from the actual (capped) transcript/tools/
    reply of the sampled trials, so the estimate reflects the real sample, not a flat anchor."""
    default_in, default_out = PRICES.get(model_id, (0.0, 0.0))
    pin = default_in if price_in is None else price_in
    pout = default_out if price_out is None else price_out
    tok_in = sum(_approx_tokens(r) for r in sample_rows)
    tok_out = len(sample_rows) * _LABEL_OUT_TOK
    usd = tok_in / 1e6 * pin + tok_out / 1e6 * pout
    return {
        "model": model_id,
        "n_trials": len(sample_rows),
        "calls": len(sample_rows),
        "approx_tokens": {"in": tok_in, "out": tok_out},
        "price_per_1M_usd": {"in": pin, "out": pout},
        "price_known": model_id in PRICES or (price_in is not None and price_out is not None),
        "estimated_usd": round(usd, 4),
    }


# ----------------------------------------------------------------------------------------------
# Keyed labeling (ONLY runs when a key is present AND the caller opted in — gated by the CLI).
# ----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class LabeledTrial:
    """One trial's independent reference label aligned with its oracle + judge predictions."""

    row: TrialRow
    reference: bool
    confidence: float
    reason: str


async def _label_one(
    row: TrialRow, question: str, threshold: float, model_id: str, api_key: str | None,
    timeout_s: float, max_retries: int,
) -> LabeledTrial:
    confirmed, conf, reason = await judge_confirms(
        judge_prompt=question,
        confidence_threshold=threshold,
        transcript=row.transcript,
        tool_calls=row.tool_calls,
        reply=row.reply,
        model_id=model_id,
        api_key=api_key,
        timeout_s=timeout_s,
        max_retries=max_retries,
    )
    return LabeledTrial(row=row, reference=bool(confirmed), confidence=float(conf), reason=reason)


async def label_sample(
    sample_rows: Sequence[TrialRow], sem_index: dict[str, dict], *, model_id: str,
    api_key: str | None, threshold: float, max_concurrency: int = 4,
    timeout_s: float = 60.0, max_retries: int = 3,
) -> list[LabeledTrial]:
    """Label every sampled trial with the INDEPENDENT model, bounded-concurrently. Each trial uses
    its probe's own judge_prompt; the labeler's own `threshold` decides the binary reference (the
    engine's resilient_generate bounds every call). Trials whose probe has no semantic question are
    skipped (they can never have a real judge verdict to score)."""
    sem = asyncio.Semaphore(max(1, max_concurrency))

    async def _guarded(row: TrialRow) -> LabeledTrial | None:
        meta = sem_index.get(row.probe_id)
        if not meta:
            return None
        async with sem:
            return await _label_one(
                row, meta["question"], threshold, model_id, api_key, timeout_s, max_retries
            )

    results = await asyncio.gather(*(_guarded(r) for r in sample_rows))
    return [r for r in results if r is not None]
