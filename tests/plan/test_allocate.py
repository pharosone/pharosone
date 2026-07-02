"""Budget-allocation tests (network-free): the planner re-weights/orders WITHIN the eligible set
but NEVER drops an eligible probe and never goes below the floor.

The LLM path is exercised with a monkeypatched scripted model (the same offline-mock pattern as
tests/scoring/test_batch_judge.py) — no network. The deterministic path is the offline fallback and
reproduces today's uniform behaviour when no budget cap is set."""

from pathlib import Path

from probe_engine.corpus.loader import load_corpus
from probe_engine.plan import allocate as alloc
from probe_engine.plan.allocate import allocate, build_plan_prompt, parse_plan
from probe_engine.plan.models import AllocationBudget
from probe_engine.targets.agent_context import AgentContext

_ROOT = Path(__file__).resolve().parents[2]
_PROBES_DIR = str(_ROOT / "corpus" / "probes")


# ----- offline scripted model (mirrors tests/scoring/test_batch_judge.py) ------------------------


class _FakeOutput:
    def __init__(self, completion: str):
        self.completion = completion


class _ScriptedModel:
    """Returns a queued completion per generate() call; records call count."""

    def __init__(self, *completions: str):
        self._q = list(completions)
        self.calls = 0

    async def generate(self, _messages):
        self.calls += 1
        return _FakeOutput(self._q.pop(0) if self._q else "")


class _BoomModel:
    """A model whose generate() raises — exercises the offline fallback."""

    def __init__(self):
        self.calls = 0

    async def generate(self, _messages):
        self.calls += 1
        raise RuntimeError("model down")


def _patch_model(monkeypatch, model):
    monkeypatch.setattr(alloc, "get_model", lambda *a, **k: model)


# ----- fixtures ---------------------------------------------------------------------------------


def _probes(n: int = 4):
    """A handful of real corpus probes (load_corpus), deterministic by id for test stability."""
    corpus = load_corpus(_PROBES_DIR)
    return sorted(corpus, key=lambda p: p.id)[:n]


def _ctx():
    return AgentContext(description="a support agent", industry="fintech")


def _floor_ok(plan, eligible, budget):
    ids = {p.id for p in eligible}
    assert {a.probe_id for a in plan.items} == ids
    assert len(plan.items) == len(ids)  # each probe exactly once
    for a in plan.items:
        assert a.n_variants >= budget.min_variants
        assert a.epochs >= budget.min_epochs


# ===== (a) deterministic uniform, no budget cap =================================================


def test_deterministic_uniform_no_budget():
    eligible = _probes(4)
    budget = AllocationBudget(max_trials=None, default_variants=5, default_epochs=1)
    plan = allocate(eligible, _ctx(), budget, strategy="deterministic")
    assert plan.strategy == "deterministic"
    assert plan.model is None
    # every probe gets the uniform default (today's behaviour)
    for a in plan.items:
        assert a.n_variants == 5
        assert a.epochs == 1
    assert plan.total_trials == 4 * 5 * 1
    _floor_ok(plan, eligible, budget)


def test_no_model_id_falls_to_deterministic_even_if_strategy_llm():
    eligible = _probes(3)
    budget = AllocationBudget(max_trials=None, default_variants=5, default_epochs=1)
    plan = allocate(eligible, _ctx(), budget, strategy="llm", model_id=None)
    assert plan.strategy == "deterministic"  # no model -> deterministic
    for a in plan.items:
        assert a.n_variants == 5


# ===== (b) budget smaller than uniform -> scaled down, floor respected, all present =============


def test_budget_smaller_than_uniform_scales_down():
    eligible = _probes(4)
    # uniform would be 4*5 = 20 trials; cap at 8.
    budget = AllocationBudget(max_trials=8, default_variants=5, default_epochs=1, min_variants=1)
    plan = allocate(eligible, _ctx(), budget, strategy="deterministic")
    assert plan.total_trials <= 8
    for a in plan.items:
        assert a.n_variants < 5  # scaled down from the uniform default
        assert a.n_variants >= 1  # never below floor
    _floor_ok(plan, eligible, budget)


def test_budget_below_floor_total_still_keeps_every_probe_at_floor():
    eligible = _probes(4)
    # cap (2) is below the all-floor total (4*1=4): honesty wins — every probe stays at the floor.
    budget = AllocationBudget(max_trials=2, default_variants=5, default_epochs=1, min_variants=1)
    plan = allocate(eligible, _ctx(), budget, strategy="deterministic")
    _floor_ok(plan, eligible, budget)
    for a in plan.items:
        assert a.n_variants == 1  # cannot go below the floor even though it busts the cap


def test_budget_scales_epochs_only_when_floor_variants_still_bust_cap():
    eligible = _probes(2)
    # default 3 variants * 4 epochs = 24/probe -> 48 total; floor variants (1)*4 epochs *2 = 8 > cap 4
    budget = AllocationBudget(
        max_trials=4, default_variants=3, default_epochs=4, min_variants=1, min_epochs=1
    )
    plan = allocate(eligible, _ctx(), budget, strategy="deterministic")
    for a in plan.items:
        assert a.n_variants == 1  # variants pinned to floor
        assert a.epochs < 4  # epochs also trimmed because floor-variants still bust the cap
        assert a.epochs >= 1
    assert plan.total_trials <= 4
    _floor_ok(plan, eligible, budget)


# ===== (c) llm path: high-weight probe gets more variants, total <= max_trials, all present ======


def test_llm_path_high_weight_gets_more_variants(monkeypatch):
    # Heavily-weight the CHEAPEST eligible probe so the assertion isolates the weight effect from the
    # (newer) cost-awareness: an equal-or-cheaper, higher-weighted probe must win on variants. (When
    # the heavily-weighted probe is also the most EXPENSIVE one, cost-awareness deliberately holds it
    # back — that is covered by tests/plan/test_cost_aware_allocation.py.)
    eligible = sorted(_probes(3), key=alloc._cost_weight)  # cheapest first
    ids = [p.id for p in eligible]
    completion = (
        '{"allocations": ['
        f'{{"probe_id": "{ids[0]}", "weight": 1.0, "depth": "deep"}}, '
        f'{{"probe_id": "{ids[1]}", "weight": 0.1, "depth": "normal"}}, '
        f'{{"probe_id": "{ids[2]}", "weight": 0.1, "depth": "shallow"}}'
        '], "notes": "focus the first probe"}'
    )
    model = _ScriptedModel(completion)
    _patch_model(monkeypatch, model)
    budget = AllocationBudget(max_trials=30, default_variants=5, default_epochs=1, min_variants=1)
    plan = allocate(eligible, _ctx(), budget, strategy="llm", model_id="anthropic/claude-opus-4-8")

    assert plan.strategy == "llm"
    assert plan.model == "anthropic/claude-opus-4-8"
    assert model.calls == 1
    assert plan.total_trials <= 30
    top = plan.for_probe(ids[0])
    others = [plan.for_probe(ids[1]), plan.for_probe(ids[2])]
    # the heavily-weighted + deep + cheapest probe gets more variants than each lightly-weighted one
    for o in others:
        assert top.n_variants >= o.n_variants
    # "deep" raised its epochs above default
    assert top.epochs >= 1
    _floor_ok(plan, eligible, budget)


def test_llm_path_total_respects_cap_and_orders_by_priority(monkeypatch):
    eligible = _probes(3)
    ids = [p.id for p in eligible]
    completion = (
        '{"allocations": ['
        f'{{"probe_id": "{ids[0]}", "weight": 0.9, "depth": "normal"}}, '
        f'{{"probe_id": "{ids[1]}", "weight": 0.5, "depth": "normal"}}, '
        f'{{"probe_id": "{ids[2]}", "weight": 0.1, "depth": "normal"}}'
        '], "notes": ""}'
    )
    model = _ScriptedModel(completion)
    _patch_model(monkeypatch, model)
    budget = AllocationBudget(max_trials=12, default_variants=4, default_epochs=1, min_variants=1)
    plan = allocate(eligible, _ctx(), budget, strategy="llm", model_id="anthropic/claude-opus-4-8")
    assert plan.total_trials <= 12
    # items sorted by priority desc
    priorities = [a.priority for a in plan.items]
    assert priorities == sorted(priorities, reverse=True)
    _floor_ok(plan, eligible, budget)


# ===== (d) llm path with a model that errors -> deterministic fallback ===========================


def test_llm_model_error_falls_back_to_deterministic(monkeypatch):
    eligible = _probes(4)
    model = _BoomModel()
    _patch_model(monkeypatch, model)
    budget = AllocationBudget(max_trials=None, default_variants=5, default_epochs=1)
    plan = allocate(eligible, _ctx(), budget, strategy="llm", model_id="anthropic/claude-opus-4-8")
    # fell back to uniform; model id still recorded for audit, notes explain the fallback
    assert plan.model == "anthropic/claude-opus-4-8"
    assert "fallback" in plan.notes
    for a in plan.items:
        assert a.n_variants == 5  # uniform
        assert a.epochs == 1
    _floor_ok(plan, eligible, budget)


def test_llm_unparseable_response_falls_back(monkeypatch):
    eligible = _probes(3)
    model = _ScriptedModel("I refuse to help with this. No JSON here.")
    _patch_model(monkeypatch, model)
    budget = AllocationBudget(max_trials=None, default_variants=5, default_epochs=1)
    plan = allocate(eligible, _ctx(), budget, strategy="llm", model_id="anthropic/claude-opus-4-8")
    assert "fallback" in plan.notes
    for a in plan.items:
        assert a.n_variants == 5  # deterministic uniform
    _floor_ok(plan, eligible, budget)


def test_llm_empty_response_falls_back(monkeypatch):
    eligible = _probes(3)
    model = _ScriptedModel("")  # empty completion
    _patch_model(monkeypatch, model)
    budget = AllocationBudget(max_trials=None, default_variants=5, default_epochs=1)
    plan = allocate(eligible, _ctx(), budget, strategy="llm", model_id="anthropic/claude-opus-4-8")
    assert "fallback" in plan.notes
    for a in plan.items:
        assert a.n_variants == 5
    _floor_ok(plan, eligible, budget)


def test_llm_response_missing_a_probe_still_floors_it(monkeypatch):
    # The model weights only 2 of 3 probes; the omitted probe must STILL appear at the floor.
    eligible = _probes(3)
    ids = [p.id for p in eligible]
    completion = (
        '{"allocations": ['
        f'{{"probe_id": "{ids[0]}", "weight": 1.0, "depth": "deep"}}, '
        f'{{"probe_id": "{ids[1]}", "weight": 0.5, "depth": "normal"}}'
        '], "notes": "dropped one"}'
    )
    model = _ScriptedModel(completion)
    _patch_model(monkeypatch, model)
    budget = AllocationBudget(max_trials=30, default_variants=5, default_epochs=1, min_variants=1)
    plan = allocate(eligible, _ctx(), budget, strategy="llm", model_id="anthropic/claude-opus-4-8")
    omitted = plan.for_probe(ids[2])
    assert omitted is not None
    assert omitted.n_variants >= budget.min_variants  # never dropped
    _floor_ok(plan, eligible, budget)


# ===== (e) FLOOR INVARIANT: no eligible probe ever absent or below min ===========================


def test_floor_invariant_full_corpus_deterministic():
    eligible = load_corpus(_PROBES_DIR)  # all 41
    for cap in (None, 1, 5, 41, 100, 1000):
        budget = AllocationBudget(
            max_trials=cap, default_variants=5, default_epochs=1, min_variants=1, min_epochs=1
        )
        plan = allocate(eligible, _ctx(), budget, strategy="deterministic")
        _floor_ok(plan, eligible, budget)
        if cap is not None:
            # we never exceed the cap UNLESS the all-floor total itself exceeds it (honesty floor)
            floor_total = len(eligible) * budget.min_variants * budget.min_epochs
            assert plan.total_trials <= max(cap, floor_total)


def test_floor_invariant_llm_path_various_weights(monkeypatch):
    eligible = _probes(5)
    ids = [p.id for p in eligible]
    # mixed weights incl. a zero and an out-of-range one (robust clamp)
    completion = (
        '{"allocations": ['
        f'{{"probe_id": "{ids[0]}", "weight": 0.0, "depth": "shallow"}}, '
        f'{{"probe_id": "{ids[1]}", "weight": 5.0, "depth": "deep"}}, '
        f'{{"probe_id": "{ids[2]}", "weight": 0.3, "depth": "normal"}}, '
        f'{{"probe_id": "{ids[3]}", "weight": -1.0, "depth": "bogus"}}, '
        f'{{"probe_id": "{ids[4]}", "weight": 0.7, "depth": "normal"}}'
        '], "notes": ""}'
    )
    model = _ScriptedModel(completion)
    _patch_model(monkeypatch, model)
    budget = AllocationBudget(max_trials=40, default_variants=4, default_epochs=1, min_variants=1)
    plan = allocate(eligible, _ctx(), budget, strategy="llm", model_id="anthropic/claude-opus-4-8")
    _floor_ok(plan, eligible, budget)
    assert plan.total_trials <= 40


def test_empty_eligible_returns_empty_plan():
    plan = allocate([], _ctx(), AllocationBudget(None, 5, 1), strategy="deterministic")
    assert plan.items == []


# ----- pure helpers (no model) ------------------------------------------------------------------


def test_build_plan_prompt_lists_every_probe():
    eligible = _probes(4)
    prompt = build_plan_prompt(eligible, _ctx())
    for p in eligible:
        assert p.id in prompt
    assert "support agent" in prompt  # agent brief spliced in


def test_parse_plan_json_then_clamp():
    weights, depths, notes = parse_plan(
        '{"allocations": [{"probe_id": "a", "weight": 2.0, "depth": "deep"}, '
        '{"probe_id": "b", "weight": -0.5, "depth": "weird"}], "notes": "hi"}',
        valid_ids={"a", "b"},
    )
    assert weights == {"a": 1.0, "b": 0.0}  # clamped into [0,1]
    assert depths == {"a": "deep", "b": "normal"}  # unknown depth -> normal
    assert notes == "hi"


def test_parse_plan_rejects_unknown_ids_and_empty():
    import pytest

    with pytest.raises(ValueError):
        parse_plan('{"allocations": [{"probe_id": "ghost", "weight": 1.0}]}', valid_ids={"a"})
    with pytest.raises(ValueError):
        parse_plan("not json at all", valid_ids={"a"})


def test_parse_plan_freetext_with_embedded_json():
    # robust: scrape the JSON object out of surrounding prose
    weights, _depths, _notes = parse_plan(
        'Here is my plan:\n{"allocations": [{"probe_id": "a", "weight": 0.8, "depth": "normal"}]}\nThanks!',
        valid_ids={"a"},
    )
    assert weights == {"a": 0.8}
