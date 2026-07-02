import math

import pytest

from probe_engine.scoring.statistics import detection_power, required_trials


def test_power_increases_with_trials():
    assert detection_power(0, 0.01) == 0.0
    assert detection_power(100, 0.01) > detection_power(10, 0.01)
    assert 0.0 < detection_power(300, 0.01) < 1.0


def test_required_trials_for_one_percent_at_95():
    # ~ ceil(ln(0.05)/ln(0.99)) = 299
    assert required_trials(0.01, 0.95) == 299


def test_required_trials_confidence_zero():
    assert required_trials(0.01, 0.0) == 0


def test_required_trials_rejects_nonpositive_target():
    with pytest.raises(ValueError):
        required_trials(0.0, 0.95)


def test_required_trials_meets_power():
    n = required_trials(0.02, 0.9)
    assert detection_power(n, 0.02) >= 0.9
    assert detection_power(n - 1, 0.02) < 0.9
