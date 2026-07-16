"""D follow-up — errored (all-samples-failed) probes must appear in the audit artifact.

run_corpus attaches errored probe ids to the returned EvidenceList.errored; build_report must carry
them into Report.errored_probes, render_markdown must show them, and render_json must include them
when present but stay byte-compatible (omit the key) when empty. Mirror of the blind_spots surfacing
guarantee — an errored probe is disclosed distinctly, never a silent pass. Fully offline."""

from probe_engine.report.builder import build_report
from probe_engine.report.model import Report
from probe_engine.report.render_json import render_json
from probe_engine.report.render_markdown import render_markdown
from probe_engine.run.executor import EvidenceList

from tests.report.test_builder import _fixtures


def test_build_report_carries_errored_from_evidence_list():
    rc, fw, cw, ev = _fixtures()
    el = EvidenceList(ev)
    el.errored = ["synth-authz-1", "synth-secret-2"]
    report = build_report(rc, fw, cw, el)
    assert report.errored_probes == ["synth-authz-1", "synth-secret-2"]


def test_plain_list_evidence_yields_empty_errored():
    # A non-EvidenceList caller (back-compat) -> no errored probes, no crash.
    rc, fw, cw, ev = _fixtures()
    report = build_report(rc, fw, cw, ev)
    assert report.errored_probes == []


def test_render_json_omits_errored_when_empty():
    rc, fw, cw, ev = _fixtures()
    assert '"errored_probes"' not in render_json(build_report(rc, fw, cw, ev))


def test_render_json_includes_errored_when_present():
    rc, fw, cw, ev = _fixtures()
    el = EvidenceList(ev)
    el.errored = ["synth-authz-1"]
    js = render_json(build_report(rc, fw, cw, el))
    assert '"errored_probes"' in js and "synth-authz-1" in js


def _bare_aggregates() -> dict:
    return {"overall_asr": 0.0, "n_probes": 0, "n_controls": 0, "n_covered": 0,
            "n_partial": 0, "n_uncovered": 0, "n_not_testable": 0, "by_severity": {},
            "n_findings_fired": 0, "by_control_verdict": {}, "overall_verdict": "no_coverage"}


def test_render_markdown_shows_errored():
    report = Report(
        scope={}, coverage=[], evidence=[], gaps=[], aggregates=_bare_aggregates(),
        errored_probes=["synth-authz-1", "synth-secret-2"],
    )
    md = render_markdown(report)
    assert "Probes errored" in md  # the errored-probes section is surfaced
    assert "synth-authz-1" in md and "synth-secret-2" in md
