"""B2 follow-up — blind-spot (skipped, not-adjudicable) probes must appear in the audit artifact.

run_corpus attaches skipped probe ids to the returned EvidenceList.blind_spots; build_report must
carry them into the Report, render_markdown must show them, and render_json must include them when
present but stay byte-compatible (omit the key) when empty. Fully offline.
"""

from probe_engine.report.builder import build_report
from probe_engine.report.model import Report
from probe_engine.report.render_json import render_json
from probe_engine.report.render_markdown import render_markdown
from probe_engine.run.executor import EvidenceList

from tests.report.test_builder import _fixtures


def test_build_report_carries_blind_spots_from_evidence_list():
    rc, fw, cw, ev = _fixtures()
    el = EvidenceList(ev)
    el.blind_spots = ["synth-authz-1", "synth-secret-2"]
    report = build_report(rc, fw, cw, el)
    assert report.blind_spots == ["synth-authz-1", "synth-secret-2"]


def test_plain_list_evidence_yields_empty_blind_spots():
    # A non-EvidenceList caller (back-compat) -> no blind spots, no crash.
    rc, fw, cw, ev = _fixtures()
    report = build_report(rc, fw, cw, ev)
    assert report.blind_spots == []


def test_render_json_omits_blind_spots_when_empty():
    rc, fw, cw, ev = _fixtures()
    assert '"blind_spots"' not in render_json(build_report(rc, fw, cw, ev))


def test_render_json_includes_blind_spots_when_present():
    rc, fw, cw, ev = _fixtures()
    el = EvidenceList(ev)
    el.blind_spots = ["synth-authz-1"]
    js = render_json(build_report(rc, fw, cw, el))
    assert '"blind_spots"' in js and "synth-authz-1" in js


def _bare_aggregates() -> dict:
    return {"overall_asr": 0.0, "n_probes": 0, "n_controls": 0, "n_covered": 0,
            "n_partial": 0, "n_uncovered": 0, "n_not_testable": 0, "by_severity": {},
            "n_findings_fired": 0, "by_control_verdict": {}, "overall_verdict": "no_coverage"}


def test_render_markdown_shows_blind_spots():
    report = Report(
        scope={}, coverage=[], evidence=[], gaps=[], aggregates=_bare_aggregates(),
        blind_spots=["synth-authz-1", "synth-secret-2"],
    )
    md = render_markdown(report)
    # the skipped-probe ids are surfaced under the blind-spots section, each on its own bullet.
    assert "## Blind spots and untested surfaces" in md
    assert "### Skipped probes (not adjudicable on this target)" in md
    assert "- synth-authz-1" in md and "- synth-secret-2" in md


def test_render_markdown_no_skipped_probes_when_empty():
    report = Report(scope={}, coverage=[], evidence=[], gaps=[], aggregates=_bare_aggregates())
    md = render_markdown(report)
    # section is always present, but with no skipped probes it reports "None", not any probe id.
    assert "## Blind spots and untested surfaces" in md
    assert "_None — every selected probe could be adjudicated on this target._" in md
    assert "synth-authz-1" not in md
