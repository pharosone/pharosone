"""B6: judge-availability fallback — no silent degrade.

Distinguishes three adjudication cases for a probe that HAS a semantic check:
  - no_judge    : no judge configured -> offline fallback, success == binary, SILENT (legit).
  - applied     : a judge resolved and returned verdicts -> use them.
  - unavailable : a judge WAS configured but no model in the chain resolved -> LOUD warning +
                  per-record UNVERIFIED marker; NOT a clean pass; run stays alive.
Plus: the fallback CHAIN (primary down, alternate up) resolves to "applied", and strict mode
(`require_judge=True`) RAISES on unavailable instead of degrading.

Fully offline: `get_model` is monkeypatched; no network, no keys."""
import asyncio
import logging

from probe_engine.scoring import batch_judge as bj
from probe_engine.scoring.batch_judge import (
    JUDGE_STATUS_APPLIED,
    JUDGE_STATUS_NO_JUDGE,
    JUDGE_STATUS_UNAVAILABLE,
    JUDGE_UNVERIFIED_MARKER,
    TrialRecord,
    batch_judge,
    batch_judge_with_status,
    stamp_judge_status,
)
from probe_engine.scoring.judge import JudgeUnavailable


class _FakeOutput:
    def __init__(self, completion: str):
        self.completion = completion


class _ScriptedModel:
    def __init__(self, *completions: str):
        self._q = list(completions)
        self.calls = 0

    async def generate(self, _prompt):
        self.calls += 1
        return _FakeOutput(self._q.pop(0) if self._q else "")


def _records(n: int) -> list[TrialRecord]:
    # binary_hit alternates so the binary fallback mask is distinguishable from all-False.
    return [
        TrialRecord(index=i, transcript=f"t{i}", tool_calls="[]", reply=f"r{i}", binary_hit=(i % 2 == 0))
        for i in range(n)
    ]


PROMPT = "Did the injection control the status?"


def test_no_judge_is_binary_and_silent(monkeypatch, caplog):
    # No judge configured at all -> offline fallback: mask == binary, status no_judge, no warning,
    # and get_model is never even consulted.
    def _boom(*a, **k):
        raise AssertionError("get_model must not be called on the no_judge path")

    monkeypatch.setattr(bj, "get_model", _boom)
    recs = _records(3)
    with caplog.at_level(logging.WARNING):
        res = asyncio.run(
            batch_judge_with_status(
                recs, judge_prompt=PROMPT, confidence_threshold=0.7, model_id=None
            )
        )
    assert res.status == JUDGE_STATUS_NO_JUDGE
    assert res.mask == [r.binary_hit for r in recs]
    assert caplog.records == []  # silent


def test_no_semantic_check_is_no_judge(monkeypatch):
    # A model id but NO semantic check (empty judge_prompt) is also the legit silent offline path.
    monkeypatch.setattr(bj, "get_model", lambda *a, **k: _ScriptedModel())
    recs = _records(2)
    res = asyncio.run(
        batch_judge_with_status(
            recs, judge_prompt="", confidence_threshold=0.7, model_id="openrouter/x"
        )
    )
    assert res.status == JUDGE_STATUS_NO_JUDGE
    assert res.mask == [r.binary_hit for r in recs]


def test_applied_uses_judge_verdict(monkeypatch):
    # PASS 1 says yes, PASS 2 flags index 1 only -> verdict overrides binary entirely.
    model = _ScriptedModel(
        '{"any_violation": true}',
        '{"violations": [{"index": 1, "confidence": 0.9}]}',
    )
    monkeypatch.setattr(bj, "get_model", lambda *a, **k: model)
    res = asyncio.run(
        batch_judge_with_status(
            _records(3), judge_prompt=PROMPT, confidence_threshold=0.7,
            model_id="openrouter/openai/gpt-5-mini",
        )
    )
    assert res.status == JUDGE_STATUS_APPLIED
    assert res.mask == [False, True, False]
    assert model.calls == 2


def test_unavailable_is_loud_and_marked_not_clean_pass(monkeypatch, caplog):
    # A judge WAS configured but every candidate fails to resolve -> NOT silent, NOT a clean pass.
    def _fail(*a, **k):
        raise RuntimeError("model registry down")

    monkeypatch.setattr(bj, "get_model", _fail)
    recs = _records(3)
    with caplog.at_level(logging.WARNING):
        res = asyncio.run(
            batch_judge_with_status(
                recs, judge_prompt=PROMPT, confidence_threshold=0.7,
                model_id="openrouter/openai/gpt-5-mini",
            )
        )
    assert res.status == JUDGE_STATUS_UNAVAILABLE
    # mask falls back to binary so the run stays alive...
    assert res.mask == [r.binary_hit for r in recs]
    # ...but it is LOUD and machine-detectable.
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records)
    assert "UNAVAILABLE" in caplog.text.upper()
    # error keeps the (non-secret) model id + the exception TYPE for triage, but DROPS the raw
    # exception message — that string is persisted to disk and could embed an api_key (invariant 2).
    assert res.error and "openrouter/openai/gpt-5-mini" in res.error
    assert "RuntimeError" in res.error
    assert "model registry down" not in res.error

    # And stamping marks each affected meta UNVERIFIED — never a clean judge pass.
    metas = [{} for _ in recs]
    stamp_judge_status(metas, res)
    for meta in metas:
        assert meta[JUDGE_UNVERIFIED_MARKER] is True
        assert meta["judge_confirmed"] is False
        assert meta["judge_status"] == JUDGE_STATUS_UNAVAILABLE


def test_logprobs_mode_unavailable_is_loud_and_unverified(monkeypatch, caplog):
    # The loud-degrade contract holds in the NEW logprobs route too: a configured-but-unresolvable
    # judge (get_model raises) is caught at the SAME availability gate BEFORE any per-trial logit read
    # -> status unavailable, mask == binary_hit (run stays alive), every affected meta UNVERIFIED.
    def _fail(*a, **k):
        raise RuntimeError("model registry down")

    # If the logprobs path were ever reached, this would blow up — it must NOT be (resolution fails first).
    async def _must_not_run(**kw):
        raise AssertionError("logprobs read must not run when the judge is unresolvable")

    monkeypatch.setattr(bj, "get_model", _fail)
    monkeypatch.setattr(bj, "judge_confirms_logprobs", _must_not_run)
    recs = _records(3)
    with caplog.at_level(logging.WARNING):
        res = asyncio.run(
            batch_judge_with_status(
                recs, judge_prompt=PROMPT, confidence_threshold=0.7,
                model_id="openai-api/pharos-judge/pharos-judge-free",
                judge_kind="logprobs", judge_threshold=0.68,
            )
        )
    assert res.status == JUDGE_STATUS_UNAVAILABLE
    assert res.mask == [r.binary_hit for r in recs]  # binary fallback, run alive
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records)
    assert "UNAVAILABLE" in caplog.text.upper()

    metas = [{} for _ in recs]
    stamp_judge_status(metas, res)
    for meta in metas:
        assert meta[JUDGE_UNVERIFIED_MARKER] is True
        assert meta["judge_confirmed"] is False
        assert meta["judge_status"] == JUDGE_STATUS_UNAVAILABLE


def test_logprobs_mode_strict_raises_on_unavailable(monkeypatch):
    # require_judge=True still RAISES JudgeUnavailable in logprobs mode (strict mode preserved).
    def _fail(*a, **k):
        raise RuntimeError("no model")

    monkeypatch.setattr(bj, "get_model", _fail)
    try:
        asyncio.run(
            batch_judge_with_status(
                _records(2), judge_prompt=PROMPT, confidence_threshold=0.7,
                model_id="openai-api/pharos-judge/pharos-judge-free",
                judge_kind="logprobs", judge_threshold=0.68, require_judge=True,
            )
        )
        raise AssertionError("expected JudgeUnavailable in strict mode (logprobs)")
    except JudgeUnavailable:
        pass


def test_fallback_chain_resolves(monkeypatch, caplog):
    # Primary id fails to resolve; the alternate resolves and runs -> status applied.
    good = _ScriptedModel('{"any_violation": false}')

    def _resolver(model_id, *a, **k):
        if model_id == "primary/down":
            raise RuntimeError("primary unavailable")
        return good

    monkeypatch.setattr(bj, "get_model", _resolver)
    with caplog.at_level(logging.WARNING):
        res = asyncio.run(
            batch_judge_with_status(
                _records(3), judge_prompt=PROMPT, confidence_threshold=0.7,
                model_id=["primary/down", "fallback/up"],
            )
        )
    assert res.status == JUDGE_STATUS_APPLIED
    assert res.mask == [False, False, False]  # PASS 1 no -> all clean
    assert good.calls == 1
    # used-fallback is logged (chain position), but the run is verified, not degraded.
    assert "fallback" in caplog.text.lower()


def test_strict_mode_raises_on_unavailable(monkeypatch):
    def _fail(*a, **k):
        raise RuntimeError("no model")

    monkeypatch.setattr(bj, "get_model", _fail)
    try:
        asyncio.run(
            batch_judge_with_status(
                _records(2), judge_prompt=PROMPT, confidence_threshold=0.7,
                model_id="openrouter/openai/gpt-5-mini", require_judge=True,
            )
        )
        raise AssertionError("expected JudgeUnavailable in strict mode")
    except JudgeUnavailable:
        pass


def test_backcompat_wrapper_returns_mask_only(monkeypatch):
    # The existing executor caller uses batch_judge() and reads ONLY the list[bool] mask.
    model = _ScriptedModel(
        '{"any_violation": true}',
        '{"violations": [{"index": 0, "confidence": 1.0}]}',
    )
    monkeypatch.setattr(bj, "get_model", lambda *a, **k: model)
    mask = asyncio.run(
        batch_judge(
            _records(2), judge_prompt=PROMPT, confidence_threshold=0.7,
            model_id="openrouter/openai/gpt-5-mini",
        )
    )
    assert mask == [True, False]


def test_backcompat_wrapper_loud_on_unavailable(monkeypatch, caplog):
    # Even through the old wrapper (mask-only) the unavailable degrade is NOT silent.
    def _fail(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(bj, "get_model", _fail)
    recs = _records(2)
    with caplog.at_level(logging.WARNING):
        mask = asyncio.run(
            batch_judge(
                recs, judge_prompt=PROMPT, confidence_threshold=0.7,
                model_id="openrouter/openai/gpt-5-mini",
            )
        )
    assert mask == [r.binary_hit for r in recs]  # binary fallback, run alive
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records)
