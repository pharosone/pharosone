"""Dual-judge calibration: is a cheap DeepSeek judge as good as the expensive GLM judge?

The upcoming example variation-policy experiment uses judge = GLM 5.2 (`z-ai/glm-5.2`), which is
~2/3 of its cost. This package measures — over a SHARED sample of real DeepSeek-SUT trial
transcripts — whether judge = DeepSeek V4 Flash (`deepseek/deepseek-v4-flash`) agrees with the GLM
judge, so the swap can be decided by a judge-agreement measurement rather than by opinion.

  * ``agreement`` — PURE, offline, unit-tested judge-agreement math (Cohen's kappa, % agreement,
    positive-class precision/recall/F1 with GLM as reference, the 2x2 confusion, the net hit-delta,
    and a by-oracle-kind breakdown). No I/O, no network.
  * ``calibrate`` — the runner: it sources the sample from EXISTING ``*.eval`` logs, runs the
    engine's OWN two-pass batch judge (``scoring.batch_judge``) twice over the identical per-probe
    batches — once with GLM, once with DeepSeek — and feeds the per-trial verdicts to ``agreement``.
"""
