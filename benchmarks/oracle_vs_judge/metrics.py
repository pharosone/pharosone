"""Pure oracle-vs-judge math (offline, unit-tested). NOTHING here calls a model or touches disk.

Two boolean signals per trial:
  * ``oracle`` — the static oracle's provisional binary hit (`metadata["binary_hit"]`);
  * ``judge``  — the LLM judge's verdict (`metadata["success"]` after the batch judge pass).

The POSITIVE class is "the attack succeeded" (hit). The two headline structures are:

  1. the oracle x judge 2x2 OVERRIDE confusion (`OverrideConfusion`): where the judge KEPT the
     oracle's verdict (agreement) vs OVERRODE it down (`oracle=hit -> judge=no`: a false positive the
     judge filtered) or up (`oracle=no -> judge=hit`: a false negative the judge caught);
  2. generic precision/recall/F1 (`PRF`) of ANY boolean predictor against ANY boolean reference —
     used in Part B to score the oracle AND the judge against the independent reference label.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class OverrideConfusion:
    """The 2x2 contingency table of (static oracle) x (LLM judge) over a set of trials.

    Cells are trial counts. The diagonals (`oracle_hit_judge_hit`, `oracle_no_judge_no`) are
    agreements; the off-diagonals are the two override directions:
      * `oracle_hit_judge_no`  — the oracle fired but the judge cleared it (false positive FILTERED);
      * `oracle_no_judge_hit`  — the oracle missed but the judge confirmed it (false negative CAUGHT).
    """

    oracle_hit_judge_hit: int   # a: oracle=hit & judge=hit  (agree positive)
    oracle_hit_judge_no: int    # b: oracle=hit & judge=no   (judge filtered an oracle false positive)
    oracle_no_judge_hit: int    # c: oracle=no  & judge=hit  (judge caught an oracle false negative)
    oracle_no_judge_no: int     # d: oracle=no  & judge=no   (agree negative)

    @property
    def n(self) -> int:
        return (
            self.oracle_hit_judge_hit
            + self.oracle_hit_judge_no
            + self.oracle_no_judge_hit
            + self.oracle_no_judge_no
        )

    @property
    def oracle_pos(self) -> int:
        """Trials the static oracle flagged as a hit."""
        return self.oracle_hit_judge_hit + self.oracle_hit_judge_no

    @property
    def judge_pos(self) -> int:
        """Trials the LLM judge confirmed as a hit."""
        return self.oracle_hit_judge_hit + self.oracle_no_judge_hit

    @property
    def agreements(self) -> int:
        return self.oracle_hit_judge_hit + self.oracle_no_judge_no

    @property
    def overrides(self) -> int:
        return self.oracle_hit_judge_no + self.oracle_no_judge_hit


def override_confusion(oracle: Sequence[bool], judge: Sequence[bool]) -> OverrideConfusion:
    """Build the oracle x judge 2x2 table from two aligned boolean sequences.

    Raises ``ValueError`` on length mismatch — the two signals must describe the SAME trials in the
    SAME order, or every downstream number is meaningless."""
    if len(oracle) != len(judge):
        raise ValueError(f"oracle/judge length mismatch: {len(oracle)} != {len(judge)}")
    a = b = c = d = 0
    for o, j in zip(oracle, judge):
        ob, jb = bool(o), bool(j)
        if ob and jb:
            a += 1
        elif ob and not jb:
            b += 1
        elif (not ob) and jb:
            c += 1
        else:
            d += 1
    return OverrideConfusion(
        oracle_hit_judge_hit=a,
        oracle_hit_judge_no=b,
        oracle_no_judge_hit=c,
        oracle_no_judge_no=d,
    )


def agreement_rate(c: OverrideConfusion) -> float:
    """Fraction of trials where the judge KEPT the oracle's verdict. Empty input -> 1.0."""
    return c.agreements / c.n if c.n else 1.0


def override_rate(c: OverrideConfusion) -> float:
    """Fraction of trials where the judge OVERRODE the oracle (either direction). Empty -> 0.0."""
    return c.overrides / c.n if c.n else 0.0


def false_positive_filtered_rate(c: OverrideConfusion) -> float:
    """Of the oracle's HITS, the fraction the judge knocked down to "no" (`oracle=hit -> judge=no` /
    oracle hits). High = the oracle over-fires and the judge is mostly filtering false positives.
    No oracle hits -> 0.0."""
    return c.oracle_hit_judge_no / c.oracle_pos if c.oracle_pos else 0.0


def false_negative_caught_rate(c: OverrideConfusion) -> float:
    """Of the oracle's MISSES, the fraction the judge promoted to "hit" (`oracle=no -> judge=hit` /
    oracle misses). High = the oracle under-fires and the judge is recovering missed breaches.
    No oracle misses -> 0.0."""
    misses = c.oracle_no_judge_hit + c.oracle_no_judge_no
    return c.oracle_no_judge_hit / misses if misses else 0.0


@dataclass(frozen=True)
class OverrideStats:
    """Every headline override number for one set of (oracle, judge) trials."""

    confusion: OverrideConfusion
    agreement_rate: float
    override_rate: float
    false_positive_filtered_rate: float
    false_negative_caught_rate: float

    def as_dict(self) -> dict:
        c = self.confusion
        return {
            "n": c.n,
            "oracle_hits": c.oracle_pos,
            "judge_hits": c.judge_pos,
            "agreements": c.agreements,
            "overrides": c.overrides,
            "agreement_rate": round(self.agreement_rate, 4),
            "override_rate": round(self.override_rate, 4),
            "fp_filtered_by_judge": c.oracle_hit_judge_no,
            "fp_filtered_rate_of_oracle_hits": round(self.false_positive_filtered_rate, 4),
            "fn_caught_by_judge": c.oracle_no_judge_hit,
            "fn_caught_rate_of_oracle_misses": round(self.false_negative_caught_rate, 4),
            "confusion_2x2": {
                "oracle_hit_judge_hit": c.oracle_hit_judge_hit,
                "oracle_hit_judge_no": c.oracle_hit_judge_no,
                "oracle_no_judge_hit": c.oracle_no_judge_hit,
                "oracle_no_judge_no": c.oracle_no_judge_no,
            },
        }


def summarize_overrides(oracle: Sequence[bool], judge: Sequence[bool]) -> OverrideStats:
    """Compute the full :class:`OverrideStats` from two aligned (oracle, judge) sequences."""
    c = override_confusion(oracle, judge)
    return OverrideStats(
        confusion=c,
        agreement_rate=agreement_rate(c),
        override_rate=override_rate(c),
        false_positive_filtered_rate=false_positive_filtered_rate(c),
        false_negative_caught_rate=false_negative_caught_rate(c),
    )


def summarize_overrides_by_group(
    oracle: Sequence[bool], judge: Sequence[bool], groups: Sequence[str]
) -> dict[str, OverrideStats]:
    """Split the trials by ``groups[i]`` (e.g. oracle kind) and summarize each group independently.

    Lets the report separate where the judge ADDS value (semantic oracles like prompt_leak / contains
    / output_pattern) from where it is REDUNDANT (high-precision side-effect oracles like tool_called).
    Raises ``ValueError`` if the three sequences are not the same length."""
    if not (len(oracle) == len(judge) == len(groups)):
        raise ValueError(
            f"oracle/judge/groups length mismatch: {len(oracle)}/{len(judge)}/{len(groups)}"
        )
    buckets: dict[str, tuple[list[bool], list[bool]]] = {}
    for o, j, g in zip(oracle, judge, groups):
        os_, js_ = buckets.setdefault(g, ([], []))
        os_.append(bool(o))
        js_.append(bool(j))
    return {g: summarize_overrides(os_, js_) for g, (os_, js_) in buckets.items()}


# ----------------------------------------------------------------------------------------------
# Precision / recall / F1 of an arbitrary boolean PREDICTOR against an arbitrary boolean REFERENCE.
# Part B uses this twice: predictor = static oracle (binary_hit), and predictor = LLM judge
# (success); reference = the independent label. The positive class is "hit".
# ----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class PRF:
    """Precision/recall/F1 of a boolean predictor vs a boolean reference, with the raw 2x2 counts.

    TP = predicted hit & reference hit; FP = predicted hit & reference no; FN = predicted no &
    reference hit; TN = predicted no & reference no. A zero denominator yields 0.0 for that
    component (no positive predictions -> precision 0.0; no positive references -> recall 0.0)."""

    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def n(self) -> int:
        return self.tp + self.fp + self.fn + self.tn

    @property
    def precision(self) -> float:
        denom = self.tp + self.fp
        return self.tp / denom if denom else 0.0

    @property
    def recall(self) -> float:
        denom = self.tp + self.fn
        return self.tp / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) > 0 else 0.0

    @property
    def accuracy(self) -> float:
        return (self.tp + self.tn) / self.n if self.n else 1.0

    def as_dict(self) -> dict:
        return {
            "n": self.n,
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "tn": self.tn,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "accuracy": round(self.accuracy, 4),
        }


def prf(pred: Sequence[bool], ref: Sequence[bool]) -> PRF:
    """Build the :class:`PRF` of ``pred`` against the reference ``ref`` (positive class = hit).

    Raises ``ValueError`` on length mismatch — predictor and reference must be aligned per trial."""
    if len(pred) != len(ref):
        raise ValueError(f"pred/ref length mismatch: {len(pred)} != {len(ref)}")
    tp = fp = fn = tn = 0
    for p, r in zip(pred, ref):
        pb, rb = bool(p), bool(r)
        if pb and rb:
            tp += 1
        elif pb and not rb:
            fp += 1
        elif (not pb) and rb:
            fn += 1
        else:
            tn += 1
    return PRF(tp=tp, fp=fp, fn=fn, tn=tn)


def prf_by_group(
    pred: Sequence[bool], ref: Sequence[bool], groups: Sequence[str]
) -> dict[str, PRF]:
    """Per-group (e.g. per oracle kind) precision/recall/F1 of ``pred`` vs ``ref``.

    Raises ``ValueError`` if the three sequences are not the same length."""
    if not (len(pred) == len(ref) == len(groups)):
        raise ValueError(
            f"pred/ref/groups length mismatch: {len(pred)}/{len(ref)}/{len(groups)}"
        )
    buckets: dict[str, tuple[list[bool], list[bool]]] = {}
    for p, r, g in zip(pred, ref, groups):
        ps, rs = buckets.setdefault(g, ([], []))
        ps.append(bool(p))
        rs.append(bool(r))
    return {g: prf(ps, rs) for g, (ps, rs) in buckets.items()}
