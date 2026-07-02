"""Judge CHUNKING + per-call timeout/degrade (judge_batch_size). Model calls are stubbed — no
network. Verifies: chunks combine with correct local->global index mapping; a chunk whose judge
call fails degrades to the binary oracle + UNVERIFIED for THAT chunk only (run never hangs); all
chunks failing -> UNAVAILABLE; and batch_size=0 reproduces the all-in-one behavior."""
import asyncio

from probe_engine.scoring import batch_judge as bj
from probe_engine.scoring.batch_judge import (
    JUDGE_STATUS_APPLIED,
    JUDGE_STATUS_UNAVAILABLE,
    JUDGE_UNVERIFIED_MARKER,
    TrialRecord,
    batch_judge_with_status,
    stamp_judge_status,
)


class _Out:
    def __init__(self, completion: str):
        self.completion = completion


class _ScriptModel:
    """Per generate() call, pop the next scripted item: a str -> completion; an Exception -> raised
    (so resilient_generate sees it and, if non-transient, raises ResilientGenerateError).

    NOTE: chunks are now judged CONCURRENTLY (asyncio.gather), so call ORDER across chunks is not
    deterministic. This queue stub is only safe when all queued items are identical (clean/fail
    cases) or there is a single chunk. Order-sensitive tests use _ContentModel below instead."""

    def __init__(self, *items):
        self._q = list(items)
        self.calls = 0

    async def generate(self, _prompt):
        self.calls += 1
        item = self._q.pop(0) if self._q else ""
        if isinstance(item, Exception):
            raise item
        return _Out(item)


class _ContentModel:
    """Order-independent stub: routes by PROMPT CONTENT, not call order — safe under concurrent
    chunks. Pass-1 prompts contain 'any_violation'; pass-2 prompts contain 'List the indices'. A
    record-marker substring in ``fail_on`` (e.g. 't2') makes that chunk's call raise (degrade)."""

    def __init__(self, *, fail_on: str | None = None):
        self.fail_on = fail_on
        self.calls = 0

    async def generate(self, prompt):
        self.calls += 1
        if self.fail_on and self.fail_on in prompt:
            raise ValueError("boom")
        if "any_violation" in prompt:  # PASS 1
            return _Out('{"any_violation": true}')
        return _Out('{"violations":[{"index":0,"confidence":1.0}]}')  # PASS 2 -> local index 0


def _recs(n, binary=False, base=0):
    return [
        TrialRecord(index=base + i, transcript=f"t{base + i}", tool_calls="[]",
                    reply=f"r{base + i}", binary_hit=binary)
        for i in range(n)
    ]


def _run(monkeypatch, model, records, **kw):
    monkeypatch.setattr(bj, "get_model", lambda *a, **k: model)
    return asyncio.run(
        batch_judge_with_status(
            records,
            judge_prompt="Did the injection succeed?",
            confidence_threshold=0.7,
            model_id="openrouter/x",
            max_retries=0,  # fail fast in tests (no backoff sleeps)
            **kw,
        )
    )


def test_chunking_maps_local_indices_back_to_global(monkeypatch):
    # 4 records, batch_size=2 -> 2 chunks. Every chunk flags its LOCAL index 0; that must map back to
    # the chunk's GLOBAL start: chunk@0 -> global 0, chunk@2 -> global 2. Order-independent (gather).
    model = _ContentModel()
    res = _run(monkeypatch, model, _recs(4), batch_size=2)
    assert res.status == JUDGE_STATUS_APPLIED
    assert res.mask == [True, False, True, False]
    assert res.unverified_indices == set()
    assert model.calls == 4  # 2 chunks x (pass1 + pass2)


def test_clean_chunk_costs_one_call(monkeypatch):
    # batch_size=2 over 4 records: both chunks clean -> PASS1 'no' short-circuits each (1 call/chunk).
    model = _ScriptModel('{"any_violation": false}', '{"any_violation": false}')
    res = _run(monkeypatch, model, _recs(4), batch_size=2)
    assert res.mask == [False, False, False, False]
    assert res.status == JUDGE_STATUS_APPLIED
    assert model.calls == 2


def test_one_chunk_degrades_others_survive(monkeypatch):
    # Chunk1 judges fine; chunk2's first call raises a non-transient error -> chunk2 degrades to its
    # BINARY fallback + UNVERIFIED, but the run completes and chunk1 keeps its judge verdict.
    recs = _recs(2, binary=False, base=0) + _recs(2, binary=True, base=2)  # t0,t1 | t2,t3 (hits)
    # The chunk containing t2/t3 fails its judge call -> degrades to binary; the t0/t1 chunk judges ok.
    model = _ContentModel(fail_on="t2")
    res = _run(monkeypatch, model, recs, batch_size=2)
    assert res.status == JUDGE_STATUS_APPLIED
    # chunk1: judge flags local 0 -> global 0; chunk2: binary fallback = [True, True] (UNVERIFIED)
    assert res.mask == [True, False, True, True]
    assert res.unverified_indices == {2, 3}


def test_all_chunks_fail_is_unavailable(monkeypatch):
    model = _ScriptModel(ValueError("boom"), ValueError("boom"))
    res = _run(monkeypatch, model, _recs(4, binary=True), batch_size=2)
    assert res.status == JUDGE_STATUS_UNAVAILABLE
    assert res.mask == [True, True, True, True]  # binary fallback
    assert res.unverified_indices == {0, 1, 2, 3}


def test_batch_size_zero_is_single_chunk(monkeypatch):
    # batch_size=0 -> one chunk over all records (today's behavior).
    model = _ScriptModel('{"any_violation": true}', '{"violations":[{"index":2,"confidence":1.0}]}')
    res = _run(monkeypatch, model, _recs(4), batch_size=0)
    assert res.mask == [False, False, True, False]
    assert model.calls == 2


def test_stamp_marks_per_record_unverified():
    # A partial-degrade result: status applied, but index 1 is unverified -> only it is marked.
    from probe_engine.scoring.batch_judge import BatchJudgeResult

    metas = [{}, {}]
    res = BatchJudgeResult(mask=[True, True], status=JUDGE_STATUS_APPLIED, unverified_indices={1})
    stamp_judge_status(metas, res)
    assert metas[0][JUDGE_UNVERIFIED_MARKER] is False and metas[0]["judge_confirmed"] is True
    assert metas[1][JUDGE_UNVERIFIED_MARKER] is True and metas[1]["judge_confirmed"] is False
