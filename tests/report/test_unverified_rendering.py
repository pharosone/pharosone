"""GUARD: report renderers must display EvidenceStatus.UNVERIFIED DISTINCTLY ('UNVERIFIED — judge
required') and never as fail/pass. Markdown shows the label in the status cell; JSON keeps the
machine-readable `status="unverified"` AND adds a human-facing `status_label`. A report with NO
unverified evidence is byte-for-byte unchanged in JSON (regression on the optional-annotation path)."""

import json

from probe_engine.domain.crosswalk import Crosswalk, CrosswalkControlRef, Mapping
from probe_engine.domain.enums import EvidenceStatus
from probe_engine.domain.evidence import Evidence
from probe_engine.domain.framework import Control, DensityThreshold, Framework
from probe_engine.domain.probe import Provenance
from probe_engine.domain.run import RunConfig, TargetConfig, Thresholds
from probe_engine.domain.taxonomy import TaxonomyTag
from probe_engine.report.builder import build_report
from probe_engine.report.render_json import UNVERIFIED_STATUS_LABEL, render_json
from probe_engine.report.render_markdown import render_markdown


def _report(status: EvidenceStatus):
    fw = Framework(id="aiuc-1", version="v1", name="AIUC-1", controls=[
        Control(id="B001", category="B", title="adv robustness",
                density_threshold=DensityThreshold(min=2, max=3)),
    ])
    cw = Crosswalk(framework="aiuc-1", framework_version="v1", entries=[
        Mapping(taxonomy_system="atlas", taxonomy_id="AML.T0051.001",
                controls=[CrosswalkControlRef(control_id="B001")])])
    ev = [Evidence(probe_id="leaky", severity="high",
                   taxonomy_tags=[TaxonomyTag(system="atlas", id="AML.T0051.001")],
                   provenance=Provenance(source="AgentDyn"),
                   n_trials=10, n_success=6, asr=0.6, status=status)]
    rc = RunConfig(target=TargetConfig(), thresholds=Thresholds(),
                   run_id="run-1", timestamp="2026-06-22T00:00:00Z")
    return build_report(rc, fw, cw, ev)


def test_markdown_renders_unverified_distinctly():
    md = render_markdown(_report(EvidenceStatus.UNVERIFIED))
    assert "UNVERIFIED — judge required" in md
    # never shown as a confident verdict for this probe row.
    row = next(line for line in md.splitlines() if line.startswith("| leaky "))
    assert "fail" not in row and "pass" not in row


def test_markdown_fail_still_renders_fail():
    md = render_markdown(_report(EvidenceStatus.FAIL))
    row = next(line for line in md.splitlines() if line.startswith("| leaky "))
    assert "fail" in row
    assert "UNVERIFIED" not in row


def test_json_unverified_has_distinct_label_and_keeps_enum():
    obj = json.loads(render_json(_report(EvidenceStatus.UNVERIFIED)))
    ev = obj["evidence"][0]
    assert ev["status"] == "unverified"  # machine-readable, distinct from pass/fail
    assert ev["status_label"] == UNVERIFIED_STATUS_LABEL
    # transparency: stats are retained, not zeroed.
    assert ev["n_success"] == 6 and ev["asr"] == 0.6


def test_json_without_unverified_is_unchanged():
    # No unverified evidence -> no status_label injected anywhere (byte-compat path).
    out = render_json(_report(EvidenceStatus.FAIL))
    assert "status_label" not in out
    obj = json.loads(out)
    assert obj["evidence"][0]["status"] == "fail"
