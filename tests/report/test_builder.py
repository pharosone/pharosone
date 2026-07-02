from probe_engine.domain.crosswalk import Crosswalk, CrosswalkControlRef, Mapping
from probe_engine.domain.evidence import Evidence
from probe_engine.domain.framework import Control, DensityThreshold, Framework
from probe_engine.domain.probe import (
    BinaryCheck,
    Evaluation,
    Probe,
    Provenance,
    Scenario,
    Turn,
)
from probe_engine.domain.run import RunConfig, TargetConfig, Thresholds
from probe_engine.domain.taxonomy import TaxonomyTag
from probe_engine.plan.models import (
    AllocationPlan,
    ProbeAllocation,
    RejectedCandidate,
    SynthesisResult,
)
from probe_engine.report.builder import build_report


def _fixtures():
    fw = Framework(
        id="aiuc-1", version="v1", name="AIUC-1",
        controls=[
            Control(id="B001", category="B", title="adv robustness",
                    density_threshold=DensityThreshold(min=2, max=3)),
            Control(id="B008", category="B", title="deploy env",
                    behaviorally_testable=False),
        ],
    )
    cw = Crosswalk(
        framework="aiuc-1", framework_version="v1",
        entries=[Mapping(taxonomy_system="atlas", taxonomy_id="AML.T0051.001",
                         controls=[CrosswalkControlRef(control_id="B001")])],
    )
    ev = [Evidence(probe_id="p1", severity="high",
                   taxonomy_tags=[TaxonomyTag(system="atlas", id="AML.T0051.001")],
                   provenance=Provenance(source="AgentDyn"),
                   n_trials=10, n_success=1, asr=0.1, status="fail")]
    rc = RunConfig(target=TargetConfig(), thresholds=Thresholds(),
                   run_id="run-1", timestamp="2026-06-22T00:00:00Z")
    return rc, fw, cw, ev


def test_report_scope_and_aggregates():
    rc, fw, cw, ev = _fixtures()
    report = build_report(rc, fw, cw, ev)
    assert report.scope["standard"] == "aiuc-1 v1"
    assert report.scope["run_id"] == "run-1"
    assert report.aggregates["n_probes"] == 1
    assert report.aggregates["overall_asr"] == 0.1
    assert report.aggregates["n_not_testable"] == 1


def test_report_gaps_exclude_not_testable():
    rc, fw, cw, ev = _fixtures()
    report = build_report(rc, fw, cw, ev)
    gap_ids = {g.control_id for g in report.gaps}
    assert "B001" in gap_ids          # partial (1/2)
    assert "B008" not in gap_ids      # not testable -> not a gap
    b001_gap = next(g for g in report.gaps if g.control_id == "B001")
    assert b001_gap.required_min == 2
    assert b001_gap.n_distinct_probes == 1


def _synth_probe(pid: str = "synth-abc123") -> Probe:
    return Probe(
        id=pid, title="t", severity="high", intent="x",
        taxonomy_tags=[{"system": "atlas", "id": "AML.T0051.001"}],
        scenario=Scenario(type="chain", turns=[Turn(role="user", seed_prompts=["x"])]),
        evaluation=Evaluation(binary=BinaryCheck(oracle="tool_called", args={"tool": "move_file"})),
        provenance=Provenance(source="llm-synthesized"),
    )


def test_report_records_plan_and_synthesis():
    rc, fw, cw, ev = _fixtures()
    plan = AllocationPlan(
        items=[ProbeAllocation(probe_id="p1", n_variants=4, epochs=2, priority=3,
                               rationale="high severity")],
        strategy="deterministic", model="anthropic/claude-opus-4-8", total_trials=8,
    )
    synth = SynthesisResult(
        accepted=[_synth_probe("synth-abc123")],
        rejected=[RejectedCandidate(raw={"id": "bad-1"}, reasons=["oracle not fireable"])],
        model="anthropic/claude-opus-4-8",
    )
    report = build_report(rc, fw, cw, ev, plan=plan, synthesis=synth)
    assert report.plan["strategy"] == "deterministic"
    assert report.plan["model"] == "anthropic/claude-opus-4-8"
    assert report.plan["items"][0]["probe_id"] == "p1"
    assert report.plan["items"][0]["n_variants"] == 4
    assert report.synthesis["accepted_ids"] == ["synth-abc123"]
    assert report.synthesis["rejected"][0]["reasons"] == ["oracle not fireable"]


def test_report_plan_and_synthesis_default_none():
    rc, fw, cw, ev = _fixtures()
    report = build_report(rc, fw, cw, ev)
    assert report.plan is None
    assert report.synthesis is None
