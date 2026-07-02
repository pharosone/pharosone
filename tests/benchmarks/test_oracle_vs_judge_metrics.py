"""Exact-value unit tests for the oracle-vs-judge math (offline, no network, no logs).

Every expected number is computed BY HAND from the definitions (not read back from the
implementation): the override 2x2 confusion + its agreement/override/filtered-FP/caught-FN rates,
and the generic precision/recall/F1 of a boolean predictor against a boolean reference.
"""

import math

import pytest

from benchmarks.oracle_vs_judge.metrics import (
    OverrideConfusion,
    PRF,
    agreement_rate,
    false_negative_caught_rate,
    false_positive_filtered_rate,
    override_confusion,
    override_rate,
    prf,
    prf_by_group,
    summarize_overrides,
    summarize_overrides_by_group,
)


# ----------------------------------------------------------------------------------------------
# Override confusion (oracle x judge).
# ----------------------------------------------------------------------------------------------
def test_override_confusion_counts_each_cell() -> None:
    # oracle: H H H H H  H N N N N
    # judge:  H H H N N  N H N N N
    #   a (oracle=hit,judge=hit)=3, b (oracle=hit,judge=no)=3, c (oracle=no,judge=hit)=1,
    #   d (oracle=no,judge=no)=3
    oracle = [True, True, True, True, True, True, False, False, False, False]
    judge = [True, True, True, False, False, False, True, False, False, False]
    c = override_confusion(oracle, judge)
    assert (
        c.oracle_hit_judge_hit,
        c.oracle_hit_judge_no,
        c.oracle_no_judge_hit,
        c.oracle_no_judge_no,
    ) == (3, 3, 1, 3)
    assert c.n == 10
    assert c.oracle_pos == 6  # 3 + 3
    assert c.judge_pos == 4   # 3 + 1
    assert c.agreements == 6  # 3 + 3
    assert c.overrides == 4   # 3 + 1


def test_override_confusion_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        override_confusion([True, False], [True])


def test_override_rates_exact() -> None:
    # Same 3/3/1/3 table.
    c = OverrideConfusion(
        oracle_hit_judge_hit=3, oracle_hit_judge_no=3, oracle_no_judge_hit=1, oracle_no_judge_no=3
    )
    # agreement = (3+3)/10 = 0.6 ; override = (3+1)/10 = 0.4
    assert agreement_rate(c) == pytest.approx(0.6)
    assert override_rate(c) == pytest.approx(0.4)
    # of 6 oracle hits, judge knocked down 3 -> 0.5 ; of 4 oracle misses, judge promoted 1 -> 0.25
    assert false_positive_filtered_rate(c) == pytest.approx(0.5)
    assert false_negative_caught_rate(c) == pytest.approx(0.25)


def test_override_empty_conventions() -> None:
    c = override_confusion([], [])
    assert c.n == 0
    assert agreement_rate(c) == pytest.approx(1.0)   # vacuous agreement
    assert override_rate(c) == pytest.approx(0.0)
    assert false_positive_filtered_rate(c) == pytest.approx(0.0)
    assert false_negative_caught_rate(c) == pytest.approx(0.0)


def test_override_no_oracle_hits_filtered_rate_zero() -> None:
    # oracle never fires; judge promotes 2 of 4 misses -> filtered_rate 0.0, caught_rate 0.5.
    oracle = [False, False, False, False]
    judge = [True, True, False, False]
    c = override_confusion(oracle, judge)
    assert c.oracle_pos == 0
    assert false_positive_filtered_rate(c) == pytest.approx(0.0)
    assert false_negative_caught_rate(c) == pytest.approx(0.5)


def test_summarize_overrides_as_dict_roundtrips_numbers() -> None:
    oracle = [True, True, True, True, True, True, False, False, False, False]
    judge = [True, True, True, False, False, False, True, False, False, False]
    d = summarize_overrides(oracle, judge).as_dict()
    assert d["n"] == 10
    assert d["oracle_hits"] == 6
    assert d["judge_hits"] == 4
    assert d["agreements"] == 6
    assert d["overrides"] == 4
    assert d["agreement_rate"] == pytest.approx(0.6)
    assert d["override_rate"] == pytest.approx(0.4)
    assert d["fp_filtered_by_judge"] == 3
    assert d["fp_filtered_rate_of_oracle_hits"] == pytest.approx(0.5)
    assert d["fn_caught_by_judge"] == 1
    assert d["fn_caught_rate_of_oracle_misses"] == pytest.approx(0.25)
    assert d["confusion_2x2"] == {
        "oracle_hit_judge_hit": 3,
        "oracle_hit_judge_no": 3,
        "oracle_no_judge_hit": 1,
        "oracle_no_judge_no": 3,
    }


def test_summarize_overrides_by_group_partitions() -> None:
    # high-precision group (judge redundant): oracle==judge always -> agreement 1.0, 0 overrides.
    # semantic group (judge filters): oracle=hit,judge=no on 2 of 2 -> agreement 0.0, 2 filtered FP.
    oracle = [True, False, True, True]
    judge = [True, False, False, False]
    groups = ["tool_called", "tool_called", "prompt_leak", "prompt_leak"]
    by = summarize_overrides_by_group(oracle, judge, groups)
    assert set(by) == {"tool_called", "prompt_leak"}

    tc = by["tool_called"]
    assert tc.confusion.n == 2
    assert tc.agreement_rate == pytest.approx(1.0)
    assert tc.confusion.overrides == 0

    pl = by["prompt_leak"]
    assert pl.confusion.n == 2
    assert pl.agreement_rate == pytest.approx(0.0)
    assert pl.confusion.oracle_hit_judge_no == 2  # both oracle hits filtered by the judge
    assert pl.false_positive_filtered_rate == pytest.approx(1.0)


def test_summarize_overrides_by_group_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        summarize_overrides_by_group([True], [True], ["a", "b"])


# ----------------------------------------------------------------------------------------------
# Precision / recall / F1 of a predictor vs an independent reference.
# ----------------------------------------------------------------------------------------------
def test_prf_exact_values() -> None:
    # pred: H H H H H  N N N N N   (5 positive predictions)
    # ref:  H H H N N  H N N N N
    #   TP=3, FP=2, FN=1, TN=4
    pred = [True, True, True, True, True, False, False, False, False, False]
    ref = [True, True, True, False, False, True, False, False, False, False]
    p = prf(pred, ref)
    assert (p.tp, p.fp, p.fn, p.tn) == (3, 2, 1, 4)
    assert p.n == 10
    # precision = 3/5 = 0.6 ; recall = 3/4 = 0.75 ; F1 = 2*.6*.75/(.6+.75) = 0.9/1.35 = 2/3
    assert p.precision == pytest.approx(0.6)
    assert p.recall == pytest.approx(0.75)
    assert p.f1 == pytest.approx(2.0 / 3.0)
    # accuracy = (TP+TN)/N = 7/10
    assert p.accuracy == pytest.approx(0.7)


def test_prf_perfect_predictor() -> None:
    ref = [True, False, True, True, False]
    p = prf(ref, ref)
    assert (p.precision, p.recall, p.f1, p.accuracy) == pytest.approx((1.0, 1.0, 1.0, 1.0))


def test_prf_zero_denominator_conventions() -> None:
    # no positive predictions AND no positive references -> 0.0 components (defined, not NaN).
    p = PRF(tp=0, fp=0, fn=0, tn=5)
    assert (p.precision, p.recall, p.f1) == (0.0, 0.0, 0.0)
    assert not any(math.isnan(x) for x in (p.precision, p.recall, p.f1))
    assert p.accuracy == pytest.approx(1.0)


def test_prf_oracle_overfires_low_precision_high_recall() -> None:
    # An over-firing oracle: predicts hit on all 6, reference says only 2 are real.
    # TP=2, FP=4, FN=0, TN=0 -> precision 1/3, recall 1.0, F1 = 2*(1/3)/(1/3+1) = (2/3)/(4/3) = 0.5
    pred = [True] * 6
    ref = [True, True, False, False, False, False]
    p = prf(pred, ref)
    assert (p.tp, p.fp, p.fn, p.tn) == (2, 4, 0, 0)
    assert p.precision == pytest.approx(1.0 / 3.0)
    assert p.recall == pytest.approx(1.0)
    assert p.f1 == pytest.approx(0.5)


def test_prf_by_group_partitions_and_scores() -> None:
    # group A: pred=[H,H], ref=[H,N] -> TP=1,FP=1 -> precision 0.5, recall 1.0
    # group B: pred=[N,N], ref=[H,H] -> TP=0,FN=2 -> precision 0.0, recall 0.0
    pred = [True, True, False, False]
    ref = [True, False, True, True]
    groups = ["A", "A", "B", "B"]
    by = prf_by_group(pred, ref, groups)
    assert by["A"].precision == pytest.approx(0.5)
    assert by["A"].recall == pytest.approx(1.0)
    assert by["B"].precision == pytest.approx(0.0)
    assert by["B"].recall == pytest.approx(0.0)
    assert by["B"].fn == 2


def test_prf_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        prf([True, False], [True])
    with pytest.raises(ValueError):
        prf_by_group([True], [True], ["a", "b"])
