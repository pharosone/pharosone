"""Pure judge-agreement math (offline, unit-tested) for the dual-judge calibration.

Two judges (the REFERENCE — GLM 5.2 — and the candidate TEST — DeepSeek V4 Flash) each emit a
binary verdict per trial (hit / not-hit). This module turns the two aligned verdict sequences into
the calibration's headline numbers. NOTHING here calls a model or touches disk — it is the testable
core, deliberately separated from the runner so the math is verified on synthetic labels with exact
expected values.

Convention (load-bearing for the whole report): the REFERENCE is GLM and the TEST is DeepSeek, and
the POSITIVE class is "hit" (the judge confirmed a genuine violation). So:

  * a ``ref_only`` cell (GLM=hit, DeepSeek=no) is a hit DeepSeek MISSED — the self-family-leniency
    direction (DeepSeek judging a DeepSeek-authored reply, under-counting the breach);
  * a ``test_only`` cell (GLM=no, DeepSeek=hit) is an EXTRA hit DeepSeek invented;
  * ``hit_delta = test_pos - ref_pos`` (= test_only - ref_only): NEGATIVE means DeepSeek confirms
    FEWER hits than GLM (the dangerous leniency), positive means it confirms more.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Confusion:
    """The symmetric 2x2 contingency table of (GLM verdict) x (DeepSeek verdict).

    Cells are counts of trials. ``both_hit``/``both_no`` are agreements; ``ref_only``/``test_only``
    are the two disagreement directions (see module docstring)."""

    both_hit: int   # n11: GLM=hit  & DeepSeek=hit
    ref_only: int   # n10: GLM=hit  & DeepSeek=no   -> DeepSeek MISSED a GLM-confirmed hit (leniency)
    test_only: int  # n01: GLM=no   & DeepSeek=hit  -> DeepSeek confirmed an EXTRA hit
    both_no: int    # n00: GLM=no   & DeepSeek=no

    @property
    def n(self) -> int:
        return self.both_hit + self.ref_only + self.test_only + self.both_no

    @property
    def ref_pos(self) -> int:
        """Hits confirmed by the reference judge (GLM)."""
        return self.both_hit + self.ref_only

    @property
    def test_pos(self) -> int:
        """Hits confirmed by the test judge (DeepSeek)."""
        return self.both_hit + self.test_only

    @property
    def agreements(self) -> int:
        return self.both_hit + self.both_no

    @property
    def disagreements(self) -> int:
        return self.ref_only + self.test_only


def confusion(ref: Sequence[bool], test: Sequence[bool]) -> Confusion:
    """Build the 2x2 table from two aligned verdict sequences (ref = GLM, test = DeepSeek).

    Raises ``ValueError`` on length mismatch — the two judges must have scored the SAME trials in
    the SAME order, or every downstream number is meaningless."""
    if len(ref) != len(test):
        raise ValueError(f"ref/test length mismatch: {len(ref)} != {len(test)}")
    n11 = n10 = n01 = n00 = 0
    for r, t in zip(ref, test):
        rb, tb = bool(r), bool(t)
        if rb and tb:
            n11 += 1
        elif rb and not tb:
            n10 += 1
        elif (not rb) and tb:
            n01 += 1
        else:
            n00 += 1
    return Confusion(both_hit=n11, ref_only=n10, test_only=n01, both_no=n00)


def percent_agreement(c: Confusion) -> float:
    """Observed agreement p_o = (both_hit + both_no) / N. Empty input -> 1.0 (no disagreements)."""
    return c.agreements / c.n if c.n else 1.0


def cohens_kappa(c: Confusion) -> float:
    """Cohen's kappa = (p_o - p_e) / (1 - p_e), correcting observed agreement for chance.

    p_e is the agreement expected if each judge labelled independently at its own marginal hit-rate.
    Range: 1.0 = perfect, 0.0 = chance-level, negative = worse than chance. Edge case: when one
    judge is constant (a marginal is 0 or 1) p_e == 1 and kappa is mathematically undefined; by the
    standard convention we return 1.0 iff the two also agree on every trial, else 0.0."""
    n = c.n
    if n == 0:
        return 1.0
    po = c.agreements / n
    p_ref_pos = c.ref_pos / n
    p_test_pos = c.test_pos / n
    pe = p_ref_pos * p_test_pos + (1.0 - p_ref_pos) * (1.0 - p_test_pos)
    if pe >= 1.0:
        return 1.0 if po >= 1.0 else 0.0
    return (po - pe) / (1.0 - pe)


def positive_class_prf(c: Confusion) -> tuple[float, float, float]:
    """Precision / recall / F1 of the TEST judge (DeepSeek) on the HIT class, taking the REFERENCE
    judge (GLM) as ground truth.

      TP = both_hit, FP = test_only (DeepSeek hit, GLM didn't), FN = ref_only (GLM hit, DeepSeek
      missed). precision = TP/(TP+FP), recall = TP/(TP+FN), F1 = harmonic mean. A zero denominator
      yields 0.0 for that component (no positive predictions / no positive references)."""
    tp, fp, fn = c.both_hit, c.test_only, c.ref_only
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    return precision, recall, f1


def hit_delta(c: Confusion) -> int:
    """Net hit-count difference DeepSeek - GLM (= test_only - ref_only). NEGATIVE = DeepSeek
    confirms fewer hits than GLM (the self-family leniency direction)."""
    return c.test_pos - c.ref_pos


@dataclass(frozen=True)
class AgreementStats:
    """Every headline agreement number for one set of paired verdicts."""

    confusion: Confusion
    percent_agreement: float
    cohens_kappa: float
    precision: float
    recall: float
    f1: float
    hit_delta: int
    ref_hits: int
    test_hits: int
    n: int

    def as_dict(self) -> dict:
        c = self.confusion
        return {
            "n": self.n,
            "ref_hits_glm": self.ref_hits,
            "test_hits_deepseek": self.test_hits,
            "hit_delta_deepseek_minus_glm": self.hit_delta,
            "percent_agreement": round(self.percent_agreement, 4),
            "cohens_kappa": round(self.cohens_kappa, 4),
            "deepseek_precision_on_hit": round(self.precision, 4),
            "deepseek_recall_on_hit": round(self.recall, 4),
            "deepseek_f1_on_hit": round(self.f1, 4),
            "confusion_2x2": {
                "both_hit": c.both_hit,
                "glm_only_deepseek_missed": c.ref_only,
                "deepseek_only_extra": c.test_only,
                "both_no": c.both_no,
            },
        }


def summarize(ref: Sequence[bool], test: Sequence[bool]) -> AgreementStats:
    """Compute the full :class:`AgreementStats` from two aligned verdict sequences."""
    c = confusion(ref, test)
    precision, recall, f1 = positive_class_prf(c)
    return AgreementStats(
        confusion=c,
        percent_agreement=percent_agreement(c),
        cohens_kappa=cohens_kappa(c),
        precision=precision,
        recall=recall,
        f1=f1,
        hit_delta=hit_delta(c),
        ref_hits=c.ref_pos,
        test_hits=c.test_pos,
        n=c.n,
    )


def summarize_by_group(
    ref: Sequence[bool], test: Sequence[bool], groups: Sequence[str]
) -> dict[str, AgreementStats]:
    """Split the trials by ``groups[i]`` (e.g. oracle kind) and summarize each group independently.

    Lets the report separate the trivially-agreeing high-precision oracles (tool_called /
    prompt_leak / output_pattern) from the semantic checks where judge divergence actually matters.
    Raises ``ValueError`` if the three sequences are not the same length."""
    if not (len(ref) == len(test) == len(groups)):
        raise ValueError(
            f"ref/test/groups length mismatch: {len(ref)}/{len(test)}/{len(groups)}"
        )
    buckets: dict[str, tuple[list[bool], list[bool]]] = {}
    for r, t, g in zip(ref, test, groups):
        rs, ts = buckets.setdefault(g, ([], []))
        rs.append(bool(r))
        ts.append(bool(t))
    return {g: summarize(rs, ts) for g, (rs, ts) in buckets.items()}
