"""Oracle-vs-Judge analysis: how much does the LLM judge change the static oracle's verdict, and
which one is actually right?

The engine decides attack success in two stages (see `scoring/oracle.py`, `scoring/batch_judge.py`,
`run/executor._apply_batch_judge`):

  1. a STATIC ORACLE computes a provisional binary hit per trial (`metadata["binary_hit"]`);
  2. an LLM JUDGE (batch, two-pass) OVERWRITES `metadata["success"]` with a semantic verdict, and
     stamps `metadata["judge_applied"] = True` iff it actually adjudicated that trial.

This package compares those two signals OFFLINE, over EXISTING `*.eval` logs, in two parts:

  * ``metrics`` — PURE, offline, unit-tested math. The oracle x judge 2x2 override confusion (and
    its override/agreement rates), and generic precision/recall/F1 of any boolean predictor against
    a boolean reference. No I/O, no network.
  * ``extract`` — read `*.eval` logs (via `inspect_ai.log.read_eval_log`) into minimal per-trial
    rows: (oracle kind, binary_hit, judge success, judge_applied, probe id, transcript/reply/tools).
    Robust to BOTH the current rich score schema and the older lean one (`success`-only).
  * ``labeler`` — Part B sampling + the INDEPENDENT reference label. A stratified <=200-trial sample
    (oversampling oracle/judge DISAGREEMENTS and positives) is labeled by an INDEPENDENT strong model
    (default `anthropic/claude-opus-4.8` — a different family from BOTH the GLM judge and the DeepSeek
    SUT, so self-family bias cannot contaminate the reference), reusing the engine's own judge logic
    (`scoring.judge.judge_confirms`) with the model id swapped. Then precision/recall/F1 of BOTH the
    static oracle and the LLM judge are computed against that reference.

`analyze` is the CLI that wires it together and writes `reports/oracle_vs_judge.{json,md}` + a sample
file. Keyed labeling is GATED: the default is a dry run (no key, prints the plan + label-cost
estimate); a real labeling pass requires `--yes` AND an OpenRouter key in the env.
"""
