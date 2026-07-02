"""Attack-success-rate statistics for rare-event detection (spec §10)."""

import math


def asr(n_success: int, n_trials: int) -> float:
    return n_success / n_trials if n_trials else 0.0


def wilson_ci(n_success: int, n_trials: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval — well-behaved for small n and extreme p (e.g. 0 successes)."""
    if n_trials == 0:
        return (0.0, 0.0)
    p = n_success / n_trials
    denom = 1 + z * z / n_trials
    center = (p + z * z / (2 * n_trials)) / denom
    margin = (z * math.sqrt(p * (1 - p) / n_trials + z * z / (4 * n_trials * n_trials))) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def detection_power(n_trials: int, target_asr: float) -> float:
    """P(observe >= 1 success | true ASR == target_asr)."""
    if n_trials <= 0:
        return 0.0
    return 1.0 - (1.0 - target_asr) ** n_trials


def required_trials(target_asr: float, confidence: float) -> int:
    """Smallest trial count whose detection power reaches `confidence`."""
    if target_asr <= 0:
        raise ValueError("target_asr must be > 0")
    if confidence <= 0:
        return 0
    return math.ceil(math.log(1 - confidence) / math.log(1 - target_asr))
