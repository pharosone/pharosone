"""Offline tests for the oracle-vs-judge runner pieces (no network, no real .eval files):

  * `extract.rows_from_eval_log` over BOTH the current rich score schema and the legacy lean one,
    using lightweight stand-in log/sample/score objects (we only touch the attributes the code reads);
  * `labeler.select_reference_sample` stratified, deterministic sampling with EXACT bucket counts;
  * `labeler.independence_blocker` / `model_family` family-collision logic;
  * `labeler.estimate_label_cost` token + USD arithmetic with exact expected values.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from benchmarks.oracle_vs_judge.extract import (
    TrialRow,
    in_window,
    parse_time,
    rows_from_eval_log,
)
from benchmarks.oracle_vs_judge.labeler import (
    estimate_label_cost,
    independence_blocker,
    model_family,
    select_reference_sample,
)


# ----------------------------------------------------------------------------------------------
# Helpers: stand-in Inspect log/sample/score objects (duck-typed to what the code reads).
# ----------------------------------------------------------------------------------------------
def _score(value, explanation, metadata):
    return SimpleNamespace(value=value, explanation=explanation, metadata=metadata)


def _sample(sid, epoch, probe_id, score, *, with_probe_meta=True):
    meta = {"probe_id": probe_id} if with_probe_meta else {}
    return SimpleNamespace(id=sid, epoch=epoch, metadata=meta, scores={"_scorer": score})


def _log(samples, model="openrouter/deepseek/deepseek-v4-flash"):
    return SimpleNamespace(eval=SimpleNamespace(model=model), samples=samples)


# ----------------------------------------------------------------------------------------------
# Extraction.
# ----------------------------------------------------------------------------------------------
def test_extract_rich_schema_reads_all_fields() -> None:
    score = _score(
        0.0,
        "oracle=contains binary=False (judge-only success decided in batch pass)",
        {
            "binary_hit": False,
            "success": True,           # judge promoted a binary miss to a hit
            "judge_applied": True,
            "judge_unverified": False,
            "transcript": "[user] do the bad thing\n[assistant] ok done",
            "tool_calls": "[]",
            "reply": "ok done",
        },
    )
    log = _log([_sample("contains-probe#0", 1, "contains-probe", score)])
    rows = rows_from_eval_log(log, "L1.eval")
    assert len(rows) == 1
    r = rows[0]
    assert r.oracle_kind == "contains"
    assert r.binary_hit is False
    assert r.judge_success is True
    assert r.judge_applied is True
    assert r.binary_from_value is False
    assert r.disagreement is True
    assert r.probe_id == "contains-probe"
    assert r.reply == "ok done"
    assert r.key() == ("L1.eval", "contains-probe#0", 1)


def test_extract_legacy_lean_schema_recovers_binary_from_value() -> None:
    # Old logs: metadata is success-only; explanation is "oracle=X success=Y"; no binary_hit key.
    score = _score(1.0, "oracle=prompt_leak success=True", {"success": True})
    log = _log([_sample("pl#0", 1, "pl-probe", score)])
    rows = rows_from_eval_log(log, "OLD.eval")
    r = rows[0]
    assert r.oracle_kind == "prompt_leak"
    assert r.binary_hit is True            # recovered from score value 1.0
    assert r.binary_from_value is True
    assert r.judge_success is True
    assert r.judge_applied is False        # no judge_applied key -> not a real judge verdict
    assert r.disagreement is False


def test_extract_judge_applied_none_is_false() -> None:
    score = _score(0.0, "oracle=tool_called binary=True (...)",
                   {"binary_hit": True, "success": True, "judge_applied": None})
    rows = rows_from_eval_log(_log([_sample("t#0", 1, "t-probe", score)]), "L.eval")
    assert rows[0].judge_applied is False
    assert rows[0].binary_hit is True
    assert rows[0].binary_from_value is False


def test_extract_probe_id_fallback_from_sample_id() -> None:
    score = _score(0.0, "oracle=contains binary=False (...)", {"binary_hit": False, "success": False})
    sample = _sample("my-probe#7", 2, None, score, with_probe_meta=False)
    rows = rows_from_eval_log(_log([sample]), "L.eval")
    assert rows[0].probe_id == "my-probe"
    assert rows[0].epoch == 2


def test_extract_oracle_kind_corpus_fallback_when_explanation_missing() -> None:
    score = _score(0.0, "no oracle token here", {"binary_hit": False, "success": False})
    rows = rows_from_eval_log(
        _log([_sample("p#0", 1, "p-probe", score)]), "L.eval",
        oracle_by_probe={"p-probe": "authz_violation"},
    )
    assert rows[0].oracle_kind == "authz_violation"


# ----------------------------------------------------------------------------------------------
# Stratified sampling.
# ----------------------------------------------------------------------------------------------
def _row(i, binary_hit, judge_success, *, judge_applied=True):
    return TrialRow(
        log_file="L.eval",
        probe_id="p",
        sample_id=f"p#{i}",
        epoch=1,
        oracle_kind="contains",
        binary_hit=binary_hit,
        judge_success=judge_success,
        judge_applied=judge_applied,
        judge_unverified=False,
        binary_from_value=False,
        transcript="",
        tool_calls="",
        reply="",
    )


def _make_pool(n_dis, n_pos, n_neg, *, judge_applied=True):
    rows = []
    i = 0
    for _ in range(n_dis):  # disagreement: oracle hit, judge no
        rows.append(_row(i, True, False, judge_applied=judge_applied))
        i += 1
    for _ in range(n_pos):  # positive agreement: both hit
        rows.append(_row(i, True, True, judge_applied=judge_applied))
        i += 1
    for _ in range(n_neg):  # negative agreement: both no
        rows.append(_row(i, False, False, judge_applied=judge_applied))
        i += 1
    return rows


def test_sampling_oversamples_disagreements_then_positives() -> None:
    pool = _make_pool(10, 50, 200)
    picked, comp = select_reference_sample(pool, cap=200, seed=0)
    # all 10 disagreements, all 50 positives, then 140 negatives to fill 200.
    assert comp.disagreements == 10
    assert comp.positive_agreements == 50
    assert comp.negative_agreements == 140
    assert comp.total == 200
    assert len(picked) == 200
    assert comp.pool == 260
    assert comp.pool_disagreements == 10
    assert comp.pool_negative_agreements == 200


def test_sampling_small_cap_stratified_50_30_20() -> None:
    # cap=5, all buckets abundant: floor(.5*5)=2 dis, floor(.3*5)=1 pos, floor(.2*5)=1 neg = 4;
    # the 1 leftover goes to disagreements first -> dis=3, pos=1, neg=1.
    pool = _make_pool(10, 50, 200)
    picked, comp = select_reference_sample(pool, cap=5, seed=0)
    assert (comp.disagreements, comp.positive_agreements, comp.negative_agreements) == (3, 1, 1)
    assert len(picked) == 5


def test_sampling_abundant_pool_hits_target_shares() -> None:
    # all three strata abundant relative to cap=200 -> exactly 100/60/40 (50/30/20 of 200).
    pool = _make_pool(300, 300, 300)
    _, comp = select_reference_sample(pool, cap=200, seed=0)
    assert (comp.disagreements, comp.positive_agreements, comp.negative_agreements) == (100, 60, 40)
    assert comp.total == 200


def test_sampling_redistributes_when_a_stratum_is_sparse() -> None:
    # No positives at all; cap=100. dis target floor(50)=50, pos floor(30)=0 (sparse), neg floor(20)=20.
    # leftover 30 -> disagreements first (room 300-50=250) -> dis=80, pos=0, neg=20.
    pool = _make_pool(300, 0, 300)
    _, comp = select_reference_sample(pool, cap=100, seed=0)
    assert (comp.disagreements, comp.positive_agreements, comp.negative_agreements) == (80, 0, 20)
    assert comp.total == 100


def test_sampling_is_deterministic_for_same_seed() -> None:
    pool = _make_pool(10, 50, 200)
    a, _ = select_reference_sample(pool, cap=200, seed=7)
    b, _ = select_reference_sample(pool, cap=200, seed=7)
    assert [r.key() for r in a] == [r.key() for r in b]


def test_sampling_excludes_non_judged_by_default() -> None:
    # 4 judged + 6 non-judged = 10 rows. Default judged-only -> pool is 4.
    pool = _make_pool(2, 1, 1, judge_applied=True) + _make_pool(3, 2, 1, judge_applied=False)
    picked, comp = select_reference_sample(pool, cap=200, seed=0)
    assert comp.pool == 4
    assert len(picked) == 4
    # with include-non-judged the whole 10-row pool is eligible.
    _, comp_all = select_reference_sample(pool, cap=200, seed=0, judge_applied_only=False)
    assert comp_all.pool == 10


# ----------------------------------------------------------------------------------------------
# Independence + cost.
# ----------------------------------------------------------------------------------------------
def test_model_family_strips_router_prefix() -> None:
    assert model_family("openrouter/anthropic/claude-opus-4.8") == "anthropic"
    assert model_family("z-ai/glm-5.2") == "z-ai"
    assert model_family("deepseek/deepseek-v4-flash") == "deepseek"
    assert model_family("openrouter/openai/gpt-5.2") == "openai"


def test_independence_blocker_none_when_independent() -> None:
    assert independence_blocker(
        "openrouter/anthropic/claude-opus-4.8", "z-ai/glm-5.2", "deepseek/deepseek-v4-flash"
    ) is None


def test_independence_blocker_flags_judge_family_collision() -> None:
    b = independence_blocker("openrouter/z-ai/glm-5.2", "z-ai/glm-5.2", "deepseek/deepseek-v4-flash")
    assert b is not None and "judge" in b


def test_independence_blocker_flags_sut_family_collision() -> None:
    b = independence_blocker(
        "deepseek/deepseek-v4-flash", "z-ai/glm-5.2", "deepseek/deepseek-v4-flash"
    )
    assert b is not None and "SUT" in b


def test_estimate_label_cost_exact() -> None:
    # Two rows, transcript exactly 400 chars (tools/reply empty):
    #   tokens_in per row = floor(400/4) + 400 overhead = 100 + 400 = 500 ; out = 90/row.
    #   tok_in = 1000, tok_out = 180.
    #   usd = 1000/1e6*10 + 180/1e6*20 = 0.01 + 0.0036 = 0.0136.
    rows = [_row(i, True, True) for i in range(2)]
    rows = [
        TrialRow(**{**r.__dict__, "transcript": "x" * 400}) for r in rows
    ]
    est = estimate_label_cost(rows, "some/model", price_in=10.0, price_out=20.0)
    assert est["approx_tokens"]["in"] == 1000
    assert est["approx_tokens"]["out"] == 180
    assert est["estimated_usd"] == pytest.approx(0.0136)
    assert est["calls"] == 2


def test_parse_time_iso_and_epoch_and_none() -> None:
    assert parse_time(None) is None
    assert parse_time("") is None
    assert parse_time("1750000000") == pytest.approx(1750000000.0)
    # an ISO date is interpreted in local time -> compare against the same construction.
    assert parse_time("2026-06-30") == pytest.approx(datetime(2026, 6, 30).timestamp())
    assert parse_time("2026-06-30T09:30:00") == pytest.approx(
        datetime(2026, 6, 30, 9, 30, 0).timestamp()
    )
    with pytest.raises(ValueError):
        parse_time("not-a-time")


def test_in_window_inclusive_open_bounds() -> None:
    assert in_window(100.0, None, None) is True          # both open
    assert in_window(100.0, 100.0, 100.0) is True        # inclusive both ends
    assert in_window(99.0, 100.0, None) is False         # before since
    assert in_window(101.0, None, 100.0) is False        # after until
    assert in_window(150.0, 100.0, 200.0) is True        # inside
    assert in_window(250.0, 100.0, 200.0) is False       # past until


def test_estimate_label_cost_caps_long_transcript() -> None:
    # transcript of 10000 chars is capped to 6000 -> tokens_in per row = 1500 + 400 = 1900.
    row = TrialRow(**{**_row(0, True, True).__dict__, "transcript": "y" * 10000})
    est = estimate_label_cost([row], "some/model", price_in=0.0, price_out=0.0)
    assert est["approx_tokens"]["in"] == 1900
    assert est["estimated_usd"] == 0.0
