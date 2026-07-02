from probe_engine.scoring.statistics import asr, wilson_ci


def test_asr_basic_and_zero_trials():
    assert asr(2, 10) == 0.2
    assert asr(0, 0) == 0.0


def test_wilson_ci_bounds_contain_point_estimate():
    lo, hi = wilson_ci(2, 10)
    assert 0.0 <= lo < 0.2 < hi <= 1.0


def test_wilson_ci_zero_successes_low_is_zero_high_positive():
    lo, hi = wilson_ci(0, 100)
    assert lo == 0.0
    assert 0.0 < hi < 0.1   # upper bound on a rare event with 100 trials


def test_wilson_ci_zero_trials():
    assert wilson_ci(0, 0) == (0.0, 0.0)
