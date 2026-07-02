"""Foundation tests for the planner data structures (network-free, pure)."""

from probe_engine.domain.probe import (
    BinaryCheck,
    Evaluation,
    Probe,
    Provenance,
    Scenario,
    Turn,
)
from probe_engine.plan.models import (
    AllocationBudget,
    AllocationPlan,
    ProbeAllocation,
    RejectedCandidate,
    SynthesisResult,
)


def _probe(pid: str) -> Probe:
    return Probe(
        id=pid,
        title="t",
        severity="high",
        intent="x",
        taxonomy_tags=[{"system": "atlas", "id": "AML.T0051.001"}],
        scenario=Scenario(type="single_turn", turns=[Turn(role="user", seed_prompts=["hi"])]),
        evaluation=Evaluation(binary=BinaryCheck(oracle="tool_called", args={"tool": "x"})),
        provenance=Provenance(source="X"),
    )


# ---- ProbeAllocation ----------------------------------------------------------------------


def test_probe_allocation_defaults_and_as_dict():
    a = ProbeAllocation(probe_id="p1", n_variants=3, epochs=2)
    assert a.priority == 0 and a.rationale == ""
    assert a.as_dict() == {
        "probe_id": "p1",
        "n_variants": 3,
        "epochs": 2,
        "priority": 0,
        "rationale": "",
    }


# ---- AllocationPlan.for_probe / as_dict --------------------------------------------------


def test_allocation_plan_for_probe_hit_and_miss():
    plan = AllocationPlan(
        items=[
            ProbeAllocation("p1", 5, 1, priority=10, rationale="high sev"),
            ProbeAllocation("p2", 2, 1),
        ],
        strategy="deterministic",
        model=None,
        total_trials=7,
    )
    hit = plan.for_probe("p1")
    assert hit is not None and hit.n_variants == 5 and hit.priority == 10
    assert plan.for_probe("does-not-exist") is None


def test_allocation_plan_as_dict_is_json_serializable():
    import json

    plan = AllocationPlan(
        items=[ProbeAllocation("p1", 5, 1, priority=10, rationale="r")],
        strategy="llm",
        model="anthropic/claude-opus-4-8",
        total_trials=5,
        notes="planned by model",
    )
    d = plan.as_dict()
    assert d["strategy"] == "llm"
    assert d["model"] == "anthropic/claude-opus-4-8"
    assert d["total_trials"] == 5
    assert d["notes"] == "planned by model"
    assert d["items"] == [
        {"probe_id": "p1", "n_variants": 5, "epochs": 1, "priority": 10, "rationale": "r"}
    ]
    # round-trips through JSON (report serialization)
    assert json.loads(json.dumps(d)) == d


# ---- AllocationBudget --------------------------------------------------------------------


def test_allocation_budget_defaults():
    b = AllocationBudget(max_trials=None, default_variants=5, default_epochs=1)
    assert b.min_variants == 1 and b.min_epochs == 1
    b2 = AllocationBudget(max_trials=100, default_variants=8, default_epochs=2, min_variants=2, min_epochs=1)
    assert b2.max_trials == 100 and b2.min_variants == 2


# ---- RejectedCandidate -------------------------------------------------------------------


def test_rejected_candidate_as_dict():
    rc = RejectedCandidate(raw={"id": "bad"}, reasons=["oracle not fireable", "unknown channel"])
    d = rc.as_dict()
    assert d["raw"] == {"id": "bad"}
    assert d["reasons"] == ["oracle not fireable", "unknown channel"]
    # as_dict copies reasons (mutating the copy must not touch the dataclass)
    d["reasons"].append("mutated")
    assert rc.reasons == ["oracle not fireable", "unknown channel"]


# ---- SynthesisResult.as_dict -------------------------------------------------------------


def test_synthesis_result_defaults():
    sr = SynthesisResult()
    assert sr.accepted == [] and sr.rejected == [] and sr.model is None and sr.notes == ""
    assert sr.as_dict() == {"model": None, "notes": "", "accepted_ids": [], "rejected": []}


def test_synthesis_result_as_dict_records_ids_and_reasons():
    import json

    sr = SynthesisResult(
        accepted=[_probe("synth-aaa"), _probe("synth-bbb")],
        rejected=[RejectedCandidate(raw={"id": "x"}, reasons=["scenario.type unknown"])],
        model="anthropic/claude-opus-4-8",
        notes="2 accepted, 1 rejected",
    )
    d = sr.as_dict()
    assert d["accepted_ids"] == ["synth-aaa", "synth-bbb"]
    assert d["model"] == "anthropic/claude-opus-4-8"
    assert d["notes"] == "2 accepted, 1 rejected"
    assert d["rejected"] == [{"raw": {"id": "x"}, "reasons": ["scenario.type unknown"]}]
    assert json.loads(json.dumps(d)) == d


def test_synthesis_result_offline_empty_accepted_with_note():
    # OFFLINE FALLBACK shape: no accepted probes, a note explaining why.
    sr = SynthesisResult(accepted=[], rejected=[], model=None, notes="no model: deterministic fallback")
    d = sr.as_dict()
    assert d["accepted_ids"] == []
    assert d["model"] is None
    assert "deterministic" in d["notes"]
