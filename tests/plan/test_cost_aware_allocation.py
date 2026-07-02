"""Cost-aware allocation tests (network-free): the LLM planner must stop preferentially inflating
the SLOWEST probes. EXTRA budget above the floor flows to CHEAP (single_turn) probes rather than
doubling a 400s/trial adaptive/chain probe — while severity still matters and the floor still holds.

The deterministic path must stay byte-for-byte unchanged (cost-awareness is LLM-path only). The
model is monkeypatched with a scripted offline completion (same pattern as test_allocate.py) — no
network."""

from probe_engine.domain.enums import ScenarioType, Severity
from probe_engine.domain.probe import (
    BinaryCheck,
    Evaluation,
    Probe,
    Provenance,
    Scenario,
    Turn,
)
from probe_engine.domain.taxonomy import TaxonomyTag
from probe_engine.plan import allocate as alloc
from probe_engine.plan.allocate import _cost_weight, allocate
from probe_engine.plan.models import AllocationBudget
from probe_engine.targets.agent_context import AgentContext


# ----- offline scripted model (mirrors tests/plan/test_allocate.py) ------------------------------


class _FakeOutput:
    def __init__(self, completion: str):
        self.completion = completion


class _ScriptedModel:
    def __init__(self, *completions: str):
        self._q = list(completions)
        self.calls = 0

    async def generate(self, _messages):
        self.calls += 1
        return _FakeOutput(self._q.pop(0) if self._q else "")


def _patch_model(monkeypatch, model):
    monkeypatch.setattr(alloc, "get_model", lambda *a, **k: model)


# ----- synthetic probes with controlled scenario kind / turn count -------------------------------


def _probe(pid: str, kind: ScenarioType, severity: Severity, *, turns: int = 1, max_turns: int = 6):
    turn_list = [Turn(role="user", poison="x") for _ in range(turns)]
    return Probe(
        id=pid,
        title=pid,
        severity=severity,
        intent="test probe intent",
        taxonomy_tags=[TaxonomyTag(system="atlas", id="AML.T0000")],
        scenario=Scenario(type=kind, turns=turn_list, max_turns=max_turns),
        evaluation=Evaluation(binary=BinaryCheck(oracle="tool_called", args={"capability": "x"})),
        provenance=Provenance(source="test"),
    )


def _ctx():
    return AgentContext(description="a support agent", industry="fintech")


def _floor_ok(plan, eligible, budget):
    ids = {p.id for p in eligible}
    assert {a.probe_id for a in plan.items} == ids
    assert len(plan.items) == len(ids)
    for a in plan.items:
        assert a.n_variants >= budget.min_variants
        assert a.epochs >= budget.min_epochs


def _alloc_for(plan, pid):
    a = plan.for_probe(pid)
    assert a is not None
    return a


# ===== _cost_weight ranks scenario kinds correctly ===============================================


def test_cost_weight_ranks_kinds():
    single = _probe("s", ScenarioType.SINGLE_TURN, Severity.HIGH, turns=1)
    chain = _probe("c", ScenarioType.CHAIN, Severity.HIGH, turns=3)
    adaptive = _probe("a", ScenarioType.ADAPTIVE, Severity.HIGH, max_turns=6)
    cs, cc, ca = _cost_weight(single), _cost_weight(chain), _cost_weight(adaptive)
    assert cs >= 1.0
    assert ca > cc > cs  # adaptive >> chain > single_turn


def test_cost_weight_scales_with_turn_count():
    one = _probe("c1", ScenarioType.CHAIN, Severity.HIGH, turns=1)
    five = _probe("c5", ScenarioType.CHAIN, Severity.HIGH, turns=5)
    assert _cost_weight(five) > _cost_weight(one)


# ===== (a) cost-awareness: an adaptive probe is NOT inflated above an equal-severity single_turn ==


def test_adaptive_not_inflated_above_single_turn_equal_severity(monkeypatch):
    # Two probes of EQUAL severity: one cheap single_turn, one expensive adaptive. The model even
    # weights the adaptive one HIGHER — but cost-awareness must keep extra budget on the cheap probe.
    single = _probe("single", ScenarioType.SINGLE_TURN, Severity.HIGH, turns=1)
    adaptive = _probe("adaptive", ScenarioType.ADAPTIVE, Severity.HIGH, max_turns=8)
    eligible = [single, adaptive]
    completion = (
        '{"allocations": ['
        '{"probe_id": "single", "weight": 0.6, "depth": "normal"}, '
        '{"probe_id": "adaptive", "weight": 0.9, "depth": "normal"}'
        '], "notes": ""}'
    )
    model = _ScriptedModel(completion)
    _patch_model(monkeypatch, model)
    budget = AllocationBudget(max_trials=40, default_variants=5, default_epochs=1, min_variants=1)
    plan = allocate(eligible, _ctx(), budget, strategy="llm", model_id="anthropic/claude-opus-4-8")

    a_single = _alloc_for(plan, "single")
    a_adaptive = _alloc_for(plan, "adaptive")
    # despite a HIGHER llm weight, the slow adaptive probe is not inflated past the cheap one
    assert a_single.n_variants >= a_adaptive.n_variants
    _floor_ok(plan, eligible, budget)


def test_severity_still_matters_within_same_cost(monkeypatch):
    # Two single_turn probes (equal cost): the CRITICAL one should still get >= the LOW one.
    crit = _probe("crit", ScenarioType.SINGLE_TURN, Severity.CRITICAL, turns=1)
    low = _probe("low", ScenarioType.SINGLE_TURN, Severity.LOW, turns=1)
    eligible = [crit, low]
    completion = (
        '{"allocations": ['
        '{"probe_id": "crit", "weight": 0.5, "depth": "normal"}, '
        '{"probe_id": "low", "weight": 0.5, "depth": "normal"}'
        '], "notes": ""}'
    )
    model = _ScriptedModel(completion)
    _patch_model(monkeypatch, model)
    budget = AllocationBudget(max_trials=40, default_variants=5, default_epochs=1, min_variants=1)
    plan = allocate(eligible, _ctx(), budget, strategy="llm", model_id="anthropic/claude-opus-4-8")
    assert _alloc_for(plan, "crit").n_variants >= _alloc_for(plan, "low").n_variants
    _floor_ok(plan, eligible, budget)


# ===== (b) max_cost caps how much any single probe is inflated ====================================


def test_max_cost_caps_single_probe_inflation(monkeypatch):
    # A single_turn probe (cost 1) heavily weighted would normally absorb a lot of variants; with a
    # tight per-probe max_cost it is shaved back so its inflated cost never exceeds the cap.
    hot = _probe("hot", ScenarioType.SINGLE_TURN, Severity.HIGH, turns=1)
    cold = _probe("cold", ScenarioType.SINGLE_TURN, Severity.HIGH, turns=1)
    eligible = [hot, cold]
    completion = (
        '{"allocations": ['
        '{"probe_id": "hot", "weight": 1.0, "depth": "normal"}, '
        '{"probe_id": "cold", "weight": 0.0, "depth": "normal"}'
        '], "notes": ""}'
    )
    model = _ScriptedModel(completion)
    _patch_model(monkeypatch, model)
    budget = AllocationBudget(max_trials=100, default_variants=5, default_epochs=1, min_variants=1)
    cap = 3.0  # cost units per probe (cost_weight of single_turn == 1.0 -> max 3 variants)
    plan = allocate(
        eligible, _ctx(), budget, strategy="llm", model_id="anthropic/claude-opus-4-8", max_cost=cap
    )
    hot_a = _alloc_for(plan, "hot")
    assert hot_a.n_variants * hot_a.epochs * _cost_weight(hot) <= cap
    _floor_ok(plan, eligible, budget)


def test_max_cost_never_drops_below_floor(monkeypatch):
    # An adaptive probe has cost_weight well above a tiny max_cost; it must still keep the floor.
    adaptive = _probe("adaptive", ScenarioType.ADAPTIVE, Severity.CRITICAL, max_turns=8)
    single = _probe("single", ScenarioType.SINGLE_TURN, Severity.LOW, turns=1)
    eligible = [adaptive, single]
    completion = (
        '{"allocations": ['
        '{"probe_id": "adaptive", "weight": 1.0, "depth": "deep"}, '
        '{"probe_id": "single", "weight": 0.5, "depth": "normal"}'
        '], "notes": ""}'
    )
    model = _ScriptedModel(completion)
    _patch_model(monkeypatch, model)
    budget = AllocationBudget(max_trials=100, default_variants=5, default_epochs=1, min_variants=1)
    plan = allocate(
        eligible, _ctx(), budget, strategy="llm", model_id="anthropic/claude-opus-4-8", max_cost=1.0
    )
    _floor_ok(plan, eligible, budget)  # adaptive kept at floor, not dropped


# ===== (c) deterministic path is byte-for-byte unchanged =========================================


def test_deterministic_path_unchanged_by_cost_awareness():
    # Mixed-cost probes; deterministic uniform must ignore cost entirely (today's behaviour).
    eligible = [
        _probe("single", ScenarioType.SINGLE_TURN, Severity.HIGH, turns=1),
        _probe("adaptive", ScenarioType.ADAPTIVE, Severity.HIGH, max_turns=8),
        _probe("chain", ScenarioType.CHAIN, Severity.HIGH, turns=4),
    ]
    budget = AllocationBudget(max_trials=None, default_variants=5, default_epochs=1)
    plan = allocate(eligible, _ctx(), budget, strategy="deterministic")
    assert plan.strategy == "deterministic"
    # every probe gets the uniform default regardless of cost
    for a in plan.items:
        assert a.n_variants == 5
        assert a.epochs == 1
    assert plan.total_trials == 3 * 5 * 1
    _floor_ok(plan, eligible, budget)


def test_max_cost_ignored_on_deterministic_path():
    # max_cost is an LLM-path knob; passing it on the deterministic path changes nothing.
    eligible = [
        _probe("single", ScenarioType.SINGLE_TURN, Severity.HIGH, turns=1),
        _probe("adaptive", ScenarioType.ADAPTIVE, Severity.HIGH, max_turns=8),
    ]
    budget = AllocationBudget(max_trials=None, default_variants=5, default_epochs=1)
    plan = allocate(eligible, _ctx(), budget, strategy="deterministic", max_cost=1.0)
    for a in plan.items:
        assert a.n_variants == 5
        assert a.epochs == 1


# ===== (d) floor invariant holds across cost-aware redistribution =================================


def test_floor_invariant_cost_aware_mixed(monkeypatch):
    eligible = [
        _probe("s1", ScenarioType.SINGLE_TURN, Severity.LOW, turns=1),
        _probe("c1", ScenarioType.CHAIN, Severity.MEDIUM, turns=5),
        _probe("a1", ScenarioType.ADAPTIVE, Severity.CRITICAL, max_turns=10),
        _probe("s2", ScenarioType.SINGLE_TURN, Severity.HIGH, turns=1),
    ]
    ids = [p.id for p in eligible]
    completion = (
        '{"allocations": ['
        f'{{"probe_id": "{ids[0]}", "weight": 0.2, "depth": "normal"}}, '
        f'{{"probe_id": "{ids[1]}", "weight": 0.9, "depth": "deep"}}, '
        f'{{"probe_id": "{ids[2]}", "weight": 1.0, "depth": "deep"}}, '
        f'{{"probe_id": "{ids[3]}", "weight": 0.4, "depth": "shallow"}}'
        '], "notes": ""}'
    )
    model = _ScriptedModel(completion)
    _patch_model(monkeypatch, model)
    budget = AllocationBudget(max_trials=60, default_variants=4, default_epochs=1, min_variants=1)
    plan = allocate(
        eligible, _ctx(), budget, strategy="llm", model_id="anthropic/claude-opus-4-8", max_cost=20.0
    )
    _floor_ok(plan, eligible, budget)
    assert plan.total_trials <= 60
