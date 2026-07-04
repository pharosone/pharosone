"""Scope exclusions (deliberately de-selected attack approaches) must appear in the audit artifact.

When the operator narrows `approaches`, the dropped probes are passed to build_report as
`scope_excluded`. The Report records `excluded_approaches` + `scope_excluded_probes` and
`scope["approaches"]`; render_markdown surfaces them under a dedicated subsection; render_json omits
the keys when empty (byte-compatible) and includes them when present. Fully offline.
"""

from probe_engine.domain.probe import (
    Applicability,
    BinaryCheck,
    Evaluation,
    Probe,
    Provenance,
    Scenario,
    Turn,
)
from probe_engine.report.builder import build_report
from probe_engine.report.model import Report
from probe_engine.report.render_json import render_json
from probe_engine.report.render_markdown import render_markdown

from tests.report.test_builder import _fixtures


def _excluded_probe(pid, scenario_type):
    return Probe(
        id=pid, title="t", severity="high", intent="x",
        taxonomy_tags=[{"system": "atlas", "id": "AML.T0051.001"}],
        applicability=Applicability(industries=["any"]),
        scenario=Scenario(type=scenario_type, turns=[Turn(role="user", seed_prompts=["hi"])]),
        evaluation=Evaluation(binary=BinaryCheck(oracle="tool_called", args={"tool": "x"})),
        provenance=Provenance(source="X"),
    )


def test_scope_defaults_carry_all_approaches():
    rc, fw, cw, ev = _fixtures()
    report = build_report(rc, fw, cw, ev)
    assert report.scope["approaches"] == ["single_turn", "chain", "adaptive"]
    assert report.excluded_approaches == []
    assert report.scope_excluded_probes == []


def test_build_report_records_excluded_approaches():
    rc, fw, cw, ev = _fixtures()
    excluded = [_excluded_probe("c1", "chain"), _excluded_probe("a1", "adaptive")]
    report = build_report(rc, fw, cw, ev, scope_excluded=excluded)
    assert report.excluded_approaches == ["adaptive", "chain"]  # sorted, de-duplicated
    assert report.scope_excluded_probes == ["a1", "c1"]


def test_render_json_omits_scope_keys_when_empty():
    rc, fw, cw, ev = _fixtures()
    js = render_json(build_report(rc, fw, cw, ev))
    assert '"excluded_approaches"' not in js
    assert '"scope_excluded_probes"' not in js


def test_render_json_includes_scope_keys_when_present():
    rc, fw, cw, ev = _fixtures()
    js = render_json(build_report(rc, fw, cw, ev, scope_excluded=[_excluded_probe("a1", "adaptive")]))
    assert '"excluded_approaches"' in js and "adaptive" in js
    assert '"scope_excluded_probes"' in js and "a1" in js


def _bare_aggregates() -> dict:
    return {"overall_asr": 0.0, "n_probes": 0, "n_controls": 0, "n_covered": 0,
            "n_partial": 0, "n_uncovered": 0, "n_not_testable": 0, "by_severity": {},
            "n_findings_fired": 0, "by_control_verdict": {}, "overall_verdict": "no_coverage"}


def test_render_markdown_shows_excluded_approaches():
    report = Report(
        scope={}, coverage=[], evidence=[], gaps=[], aggregates=_bare_aggregates(),
        excluded_approaches=["adaptive", "chain"], scope_excluded_probes=["a1", "c1", "c2"],
    )
    md = render_markdown(report)
    assert "### Approaches not tested (scope choice)" in md
    assert "- **adaptive** — not run" in md and "- **chain** — not run" in md
    assert "3 probe(s) excluded by scope" in md
    # exec-summary line names the excluded families
    assert "Approaches not tested (scope choice)" in md


def test_render_markdown_no_exclusions_reports_all_run():
    report = Report(scope={}, coverage=[], evidence=[], gaps=[], aggregates=_bare_aggregates())
    md = render_markdown(report)
    assert "### Approaches not tested (scope choice)" in md
    assert "_None — every in-scope approach (single-turn / chain / adaptive) was run._" in md
