import json

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
from probe_engine.report.render_json import render_json
from probe_engine.report.render_markdown import render_markdown


def _report(*, plan=None, synthesis=None):
    fw = Framework(id="aiuc-1", version="v1", name="AIUC-1", controls=[
        Control(id="B001", category="B", title="adv robustness",
                density_threshold=DensityThreshold(min=2, max=3)),
        Control(id="B008", category="B", title="deploy env", behaviorally_testable=False),
    ])
    cw = Crosswalk(framework="aiuc-1", framework_version="v1", entries=[
        Mapping(taxonomy_system="atlas", taxonomy_id="AML.T0051.001",
                controls=[CrosswalkControlRef(control_id="B001")])])
    ev = [Evidence(probe_id="p1", severity="high",
                   taxonomy_tags=[TaxonomyTag(system="atlas", id="AML.T0051.001")],
                   provenance=Provenance(source="AgentDyn"),
                   n_trials=10, n_success=1, asr=0.1, status="fail")]
    rc = RunConfig(target=TargetConfig(), thresholds=Thresholds(),
                   run_id="run-1", timestamp="2026-06-22T00:00:00Z")
    return build_report(rc, fw, cw, ev, plan=plan, synthesis=synthesis)


def _synth_probe(pid="synth-abc123"):
    return Probe(
        id=pid, title="t", severity="high", intent="x",
        taxonomy_tags=[{"system": "atlas", "id": "AML.T0051.001"}],
        scenario=Scenario(type="chain", turns=[Turn(role="user", seed_prompts=["x"])]),
        evaluation=Evaluation(binary=BinaryCheck(oracle="tool_called", args={"tool": "move_file"})),
        provenance=Provenance(source="llm-synthesized"),
    )


def _plan():
    return AllocationPlan(
        items=[ProbeAllocation(probe_id="p1", n_variants=4, epochs=2, priority=3,
                               rationale="high severity")],
        strategy="llm", model="anthropic/claude-opus-4-8", total_trials=8,
    )


def _synthesis():
    return SynthesisResult(
        accepted=[_synth_probe("synth-abc123")],
        rejected=[RejectedCandidate(raw={"id": "bad-1"}, reasons=["oracle not fireable"])],
        model="anthropic/claude-opus-4-8",
    )


def test_render_json_roundtrips():
    data = json.loads(render_json(_report()))
    assert data["scope"]["run_id"] == "run-1"
    assert data["aggregates"]["n_probes"] == 1
    assert any(c["control_id"] == "B001" for c in data["coverage"])


def test_render_markdown_has_sections_and_not_testable_note():
    md = render_markdown(_report())
    assert "## Executive summary" in md
    assert "## AIUC-1 control coverage" in md
    assert "## Findings" in md
    assert "## Blind spots and untested surfaces" in md
    assert "B001" in md
    assert "B008" in md
    assert "non-behavioral" in md.lower()  # B008 note (§5.3)
    # B008 is honestly NOT TESTABLE, never FAIL (§5.3): its coverage row carries the text status.
    b008_row = next(line for line in md.splitlines() if line.startswith("| B008 "))
    assert "NOT TESTABLE" in b008_row
    assert "FAIL" not in b008_row


def test_render_markdown_no_plan_or_synthesis_sections_when_absent():
    md = render_markdown(_report())
    assert "## Run plan" not in md
    assert "## Synthesized probes" not in md


def test_render_markdown_deterministic_when_plan_synthesis_none():
    # The default report (plan=None, synthesis=None) renders without the appendix sections, and is
    # deterministic across builds (no plan/synthesis bleed-through, stable ordering).
    md = render_markdown(_report())
    assert "## Executive summary" in md
    assert "## Run plan" not in md and "## Synthesized probes" not in md
    # Re-render the same report twice -> identical (deterministic).
    assert render_markdown(_report()) == md


def test_render_markdown_shows_plan_and_synthesis_sections():
    md = render_markdown(_report(plan=_plan(), synthesis=_synthesis()))
    assert "## Run plan" in md
    assert "llm" in md                      # strategy
    assert "anthropic/claude-opus-4-8" in md  # planner model
    assert "p1" in md                       # per-probe allocation row
    assert "## Synthesized probes" in md
    assert "synth-abc123" in md             # accepted id
    assert "oracle not fireable" in md      # rejected reason


def test_render_json_includes_plan_and_synthesis_when_present():
    data = json.loads(render_json(_report(plan=_plan(), synthesis=_synthesis())))
    assert data["plan"]["strategy"] == "llm"
    assert data["plan"]["items"][0]["probe_id"] == "p1"
    assert data["synthesis"]["accepted_ids"] == ["synth-abc123"]
    assert data["synthesis"]["rejected"][0]["reasons"] == ["oracle not fireable"]


def test_render_json_omits_plan_and_synthesis_keys_when_absent():
    # Byte-compat: a planner/synthesis-free report's JSON has NO plan/synthesis keys at all.
    data = json.loads(render_json(_report()))
    assert "plan" not in data
    assert "synthesis" not in data
