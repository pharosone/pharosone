"""Exact-value unit tests for the dual-judge agreement math (offline, no network).

Every expected number is computed by hand from the definitions, not read back from the
implementation — the point is to pin the LOGIC (kappa, precision/recall/F1, confusion, hit-delta),
not merely that a float of the right type comes out.
"""

import math

import pytest

from benchmarks.judge_calibration.agreement import (
    Confusion,
    cohens_kappa,
    confusion,
    hit_delta,
    percent_agreement,
    positive_class_prf,
    summarize,
    summarize_by_group,
)


def test_confusion_counts_each_cell() -> None:
    # GLM (ref):      T T T T T T F F F F
    # DeepSeek(test): T T T T F F T F F F
    # both_hit=4, ref_only=2, test_only=1, both_no=3
    ref = [True, True, True, True, True, True, False, False, False, False]
    test = [True, True, True, True, False, False, True, False, False, False]
    c = confusion(ref, test)
    assert (c.both_hit, c.ref_only, c.test_only, c.both_no) == (4, 2, 1, 3)
    assert c.n == 10
    assert c.ref_pos == 6 and c.test_pos == 5
    assert c.agreements == 7 and c.disagreements == 3


def test_confusion_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        confusion([True, False], [True])


def test_percent_agreement_and_kappa_exact() -> None:
    # Same 4/2/1/3 table. p_o = (4+3)/10 = 0.7.
    # marginals: p_ref_pos = 6/10 = 0.6, p_test_pos = 5/10 = 0.5.
    # p_e = 0.6*0.5 + 0.4*0.5 = 0.30 + 0.20 = 0.50.
    # kappa = (0.7 - 0.5) / (1 - 0.5) = 0.2 / 0.5 = 0.4.
    c = Confusion(both_hit=4, ref_only=2, test_only=1, both_no=3)
    assert percent_agreement(c) == pytest.approx(0.7)
    assert cohens_kappa(c) == pytest.approx(0.4)


def test_positive_class_prf_exact() -> None:
    # TP=4, FP=test_only=1, FN=ref_only=2.
    # precision = 4/5 = 0.8 ; recall = 4/6 = 2/3 ; F1 = 2PR/(P+R) = 16/22 = 8/11.
    c = Confusion(both_hit=4, ref_only=2, test_only=1, both_no=3)
    precision, recall, f1 = positive_class_prf(c)
    assert precision == pytest.approx(0.8)
    assert recall == pytest.approx(2.0 / 3.0)
    assert f1 == pytest.approx(8.0 / 11.0)


def test_hit_delta_sign_is_leniency_direction() -> None:
    # GLM confirms 6 hits, DeepSeek confirms 5 -> net -1 (DeepSeek under-counts = leniency).
    c = Confusion(both_hit=4, ref_only=2, test_only=1, both_no=3)
    assert hit_delta(c) == -1
    # symmetric: DeepSeek over-counts -> positive delta.
    assert hit_delta(Confusion(both_hit=4, ref_only=1, test_only=3, both_no=2)) == 2


def test_perfect_agreement() -> None:
    ref = [True, False, True, False]
    c = summarize(ref, ref)
    assert c.percent_agreement == pytest.approx(1.0)
    assert c.cohens_kappa == pytest.approx(1.0)
    assert (c.precision, c.recall, c.f1) == pytest.approx((1.0, 1.0, 1.0))
    assert c.hit_delta == 0


def test_kappa_constant_judge_edge_case() -> None:
    # Both judges say "hit" on every trial: p_e == 1, kappa undefined -> convention returns 1.0
    # because they also agree on every trial.
    c_all_hit = Confusion(both_hit=3, ref_only=0, test_only=0, both_no=0)
    assert cohens_kappa(c_all_hit) == pytest.approx(1.0)
    # One judge constant-hit, the other not: p_e == 1 but p_o < 1 -> convention returns 0.0.
    # ref all hit, test mixed: both_hit=2, ref_only=1.
    c_mixed = Confusion(both_hit=2, ref_only=1, test_only=0, both_no=0)
    assert cohens_kappa(c_mixed) == pytest.approx(0.0)


def test_kappa_total_disagreement_is_negative_one() -> None:
    # ref=[T,T,F,F], test=[F,F,T,T]: both_hit=0, ref_only=2, test_only=2, both_no=0.
    # p_o = 0 ; p_ref_pos = p_test_pos = 0.5 ; p_e = 0.5 ; kappa = (0-0.5)/0.5 = -1.0.
    c = Confusion(both_hit=0, ref_only=2, test_only=2, both_no=0)
    assert cohens_kappa(c) == pytest.approx(-1.0)
    assert percent_agreement(c) == pytest.approx(0.0)


def test_empty_input_conventions() -> None:
    c = confusion([], [])
    assert c.n == 0
    assert percent_agreement(c) == pytest.approx(1.0)
    assert cohens_kappa(c) == pytest.approx(1.0)


def test_prf_zero_denominator_conventions() -> None:
    # No positive predictions and no positive references at all -> all 0.0 (defined, not NaN).
    c = Confusion(both_hit=0, ref_only=0, test_only=0, both_no=5)
    precision, recall, f1 = positive_class_prf(c)
    assert (precision, recall, f1) == (0.0, 0.0, 0.0)
    assert not any(math.isnan(x) for x in (precision, recall, f1))


def test_summarize_by_group_partitions_and_scores() -> None:
    # Two oracle groups; each must be scored only over its own trials.
    #   group "prompt_leak": ref=[T,T], test=[T,T]              -> perfect, kappa 1.0, delta 0
    #   group "contains":    ref=[T,T,F,F], test=[F,F,T,T]      -> total disagreement, kappa -1.0
    ref = [True, True, True, True, False, False]
    test = [True, True, False, False, True, True]
    groups = ["prompt_leak", "prompt_leak", "contains", "contains", "contains", "contains"]
    by = summarize_by_group(ref, test, groups)
    assert set(by) == {"prompt_leak", "contains"}

    pl = by["prompt_leak"]
    assert pl.n == 2
    assert pl.cohens_kappa == pytest.approx(1.0)
    assert pl.hit_delta == 0

    co = by["contains"]
    assert co.n == 4
    assert (co.confusion.both_hit, co.confusion.ref_only, co.confusion.test_only,
            co.confusion.both_no) == (0, 2, 2, 0)
    assert co.cohens_kappa == pytest.approx(-1.0)
    assert co.hit_delta == 0


def test_summarize_by_group_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        summarize_by_group([True], [True], ["a", "b"])


def test_summarize_as_dict_roundtrips_key_numbers() -> None:
    ref = [True, True, True, True, True, True, False, False, False, False]
    test = [True, True, True, True, False, False, True, False, False, False]
    d = summarize(ref, test).as_dict()
    assert d["n"] == 10
    assert d["ref_hits_glm"] == 6
    assert d["test_hits_deepseek"] == 5
    assert d["hit_delta_deepseek_minus_glm"] == -1
    assert d["cohens_kappa"] == pytest.approx(0.4)
    assert d["deepseek_precision_on_hit"] == pytest.approx(0.8)
    assert d["confusion_2x2"] == {
        "both_hit": 4,
        "glm_only_deepseek_missed": 2,
        "deepseek_only_extra": 1,
        "both_no": 3,
    }
