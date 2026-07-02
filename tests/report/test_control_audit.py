"""Exact-value tests for the cabinet-facing report views: per-control AIUC-1 audit verdicts,
stats-only findings, and the aggregate verdict rollup.

These assert the actual DERIVATION logic (verdict priority, auto-close gating, control mapping,
Wilson-CI passthrough), not the mere presence of fields. All fixtures are fully deterministic.
"""

import json

import pytest

from probe_engine.domain.crosswalk import Crosswalk, CrosswalkControlRef, Mapping
from probe_engine.domain.enums import EvidenceStatus
from probe_engine.domain.evidence import Evidence, Trial
from probe_engine.domain.framework import Control, DensityThreshold, Framework
from probe_engine.domain.probe import Provenance
from probe_engine.domain.run import RunConfig, TargetConfig, Thresholds
from probe_engine.domain.taxonomy import TaxonomyTag
from probe_engine.report.builder import build_report
from probe_engine.report.model import ControlVerdict
from probe_engine.report.render_json import render_json
from probe_engine.report.render_markdown import render_markdown
from probe_engine.scoring.aggregate import aggregate_trials
from probe_engine.scoring.statistics import asr as compute_asr
from probe_engine.scoring.statistics import wilson_ci


# Each control gets a distinct ATLAS coordinate so a probe reaches exactly one control (except the
# leverage case), which lets us pin the verdict per control unambiguously.
_FRAMEWORK = Framework(
    id="aiuc-1", version="v1", name="AIUC-1",
    controls=[
        Control(id="CPASS", category="B", title="pass control",
                density_threshold=DensityThreshold(min=2, max=3)),
        Control(id="CFAIL", category="B", title="fail control"),
        Control(id="CUNV", category="B", title="unverified control"),
        Control(id="CPART", category="B", title="partial control",
                density_threshold=DensityThreshold(min=2, max=3)),
        Control(id="CINSUF", category="B", title="insufficient control"),
        Control(id="CFAILMIX", category="B", title="fail-dominates control"),
        Control(id="CUNVMIX", category="B", title="unverified-over-pass control"),
        Control(id="CNT", category="B", title="never reached control"),
        Control(id="CNTBL", category="B", title="not-testable control",
                behaviorally_testable=False),
    ],
)

_CROSSWALK = Crosswalk(
    framework="aiuc-1", framework_version="v1",
    entries=[
        Mapping(taxonomy_system="atlas", taxonomy_id="T.PASS",
                controls=[CrosswalkControlRef(control_id="CPASS")]),
        Mapping(taxonomy_system="atlas", taxonomy_id="T.FAIL",
                controls=[CrosswalkControlRef(control_id="CFAIL")]),
        Mapping(taxonomy_system="atlas", taxonomy_id="T.UNV",
                controls=[CrosswalkControlRef(control_id="CUNV")]),
        Mapping(taxonomy_system="atlas", taxonomy_id="T.PART",
                controls=[CrosswalkControlRef(control_id="CPART")]),
        Mapping(taxonomy_system="atlas", taxonomy_id="T.INSUF",
                controls=[CrosswalkControlRef(control_id="CINSUF")]),
        Mapping(taxonomy_system="atlas", taxonomy_id="T.FAILMIX",
                controls=[CrosswalkControlRef(control_id="CFAILMIX")]),
        Mapping(taxonomy_system="atlas", taxonomy_id="T.UNVMIX",
                controls=[CrosswalkControlRef(control_id="CUNVMIX")]),
        # a coordinate whose only control is ABSENT from the framework -> must be dropped from
        # findings.mapped_controls (framework filter).
        Mapping(taxonomy_system="atlas", taxonomy_id="T.GHOST",
                controls=[CrosswalkControlRef(control_id="CGHOST")]),
    ],
)


def _ev(probe_id, tax_ids, status, *, severity="high", asr=0.0, n_trials=10, n_success=0):
    return Evidence(
        probe_id=probe_id, severity=severity,
        taxonomy_tags=[TaxonomyTag(system="atlas", id=t) for t in tax_ids],
        provenance=Provenance(source="test"),
        n_trials=n_trials, n_success=n_success, asr=asr, status=status,
    )


def _run_config():
    return RunConfig(target=TargetConfig(), thresholds=Thresholds(),
                     run_id="run-1", timestamp="2026-06-22T00:00:00Z")


def _evidence_list():
    return [
        _ev("pass-a", ["T.PASS"], EvidenceStatus.PASS),
        _ev("pass-b", ["T.PASS"], EvidenceStatus.PASS),               # 2 distinct passes -> PASSED
        _ev("fail-1", ["T.FAIL"], EvidenceStatus.FAIL, severity="critical", asr=0.4, n_success=4),
        _ev("unv-1", ["T.UNV"], EvidenceStatus.UNVERIFIED, asr=0.2, n_success=2),
        _ev("part-1", ["T.PART"], EvidenceStatus.PASS),               # 1 pass, min 2 -> PARTIAL
        _ev("insuf-1", ["T.INSUF"], EvidenceStatus.INSUFFICIENT_POWER),
        # fail dominates over a co-mapped pass
        _ev("mix-fail", ["T.FAILMIX"], EvidenceStatus.FAIL, asr=0.5, n_success=5),
        _ev("mix-pass", ["T.FAILMIX"], EvidenceStatus.PASS),
        # unverified dominates over pass (no fail present)
        _ev("mix-unv", ["T.UNVMIX"], EvidenceStatus.UNVERIFIED, asr=0.2, n_success=2),
        _ev("mix-pass2", ["T.UNVMIX"], EvidenceStatus.PASS),
        # a probe whose taxonomy maps only to a control absent from the framework
        _ev("ghost-1", ["T.GHOST"], EvidenceStatus.FAIL, severity="low", asr=0.3, n_success=3),
    ]


def _audits():
    report = build_report(_run_config(), _FRAMEWORK, _CROSSWALK, _evidence_list())
    return {a.control_id: a for a in report.controls}


def test_verdict_per_control_is_correct():
    a = _audits()
    assert a["CPASS"].verdict is ControlVerdict.PASSED
    assert a["CFAIL"].verdict is ControlVerdict.FAILED
    assert a["CUNV"].verdict is ControlVerdict.UNVERIFIED
    assert a["CPART"].verdict is ControlVerdict.PARTIAL
    assert a["CINSUF"].verdict is ControlVerdict.INSUFFICIENT_EVIDENCE
    assert a["CFAILMIX"].verdict is ControlVerdict.FAILED       # fail wins over co-mapped pass
    assert a["CUNVMIX"].verdict is ControlVerdict.UNVERIFIED    # unverified wins over pass (no fail)
    assert a["CNT"].verdict is ControlVerdict.NOT_TESTED        # testable, zero probes
    assert a["CNTBL"].verdict is ControlVerdict.NOT_TESTABLE


def test_only_passed_controls_are_auto_closeable():
    a = _audits()
    assert a["CPASS"].auto_closeable is True
    for cid in ("CFAIL", "CUNV", "CPART", "CINSUF", "CFAILMIX", "CUNVMIX", "CNT", "CNTBL"):
        assert a[cid].auto_closeable is False, cid


def test_passed_control_reports_density_and_supporting_probes():
    a = _audits()["CPASS"]
    assert a.n_distinct_probes == 2
    assert a.required_min == 2
    assert a.density_met is True
    # supporting probes are sorted by id and carry their (passing) status.
    assert [s.probe_id for s in a.supporting_probes] == ["pass-a", "pass-b"]
    assert all(s.status is EvidenceStatus.PASS for s in a.supporting_probes)


def test_failed_verdict_reason_counts_failing_probes():
    a = _audits()["CFAILMIX"]
    # exactly one of the two co-mapped probes failed.
    assert a.verdict_reason == "1 probe(s) demonstrated a vulnerability (fail)."
    assert a.aggregate_asr == 0.5   # max asr among contributions (mix-fail=0.5, mix-pass=0.0)


def test_not_testable_control_is_never_a_failure():
    a = _audits()["CNTBL"]
    assert a.verdict is ControlVerdict.NOT_TESTABLE
    assert a.behaviorally_testable is False
    assert a.auto_closeable is False
    assert a.n_distinct_probes == 0
    assert "non-behavioral" in a.verdict_reason.lower()


def test_verdict_rollup_and_overall_are_exact():
    report = build_report(_run_config(), _FRAMEWORK, _CROSSWALK, _evidence_list())
    rollup = report.aggregates["by_control_verdict"]
    assert rollup == {
        "passed": 1,        # CPASS
        "failed": 2,        # CFAIL, CFAILMIX
        "unverified": 2,    # CUNV, CUNVMIX
        "insufficient_evidence": 1,  # CINSUF
        "partial": 1,       # CPART
        "not_tested": 1,    # CNT
        "not_testable": 1,  # CNTBL
    }
    # a single failed control makes the whole run fail.
    assert report.aggregates["overall_verdict"] == "fail"


def test_findings_map_controls_and_drop_ghost_control():
    report = build_report(_run_config(), _FRAMEWORK, _CROSSWALK, _evidence_list())
    by_id = {f.probe_id: f for f in report.findings}
    assert by_id["fail-1"].mapped_controls == ["CFAIL"]
    assert by_id["pass-a"].mapped_controls == ["CPASS"]
    # CGHOST is not in the framework -> filtered out, leaving an empty mapping (never fabricated).
    assert by_id["ghost-1"].mapped_controls == []
    # taxonomy coordinate is still carried on the finding for provenance.
    assert by_id["ghost-1"].taxonomy == [{"system": "atlas", "id": "T.GHOST", "name": None}]


def test_findings_sorted_worst_severity_first_then_id():
    report = build_report(_run_config(), _FRAMEWORK, _CROSSWALK, _evidence_list())
    # critical (fail-1) first; low (ghost-1) last; the many 'high' ones in between, id-sorted.
    assert report.findings[0].probe_id == "fail-1"          # only critical
    assert report.findings[-1].probe_id == "ghost-1"         # only low
    highs = [f.probe_id for f in report.findings if f.severity.value == "high"]
    assert highs == sorted(highs)


def test_finding_reproduces_exact_asr_and_wilson_ci():
    # 3 successes over 10 trials, aggregated the real way -> the finding must carry the SAME ASR and
    # Wilson interval the statistics module computes for those counts.
    trials = [Trial(variant_id=f"v{i}", epoch=0, success=(i < 3)) for i in range(10)]
    ev = aggregate_trials(
        probe_id="wilson-probe", severity="high",
        taxonomy_tags=[TaxonomyTag(system="atlas", id="T.FAIL")],
        control_overrides=[], provenance=Provenance(source="test"),
        trials=trials, thresholds=Thresholds(),
    )
    report = build_report(_run_config(), _FRAMEWORK, _CROSSWALK, [ev])
    f = next(f for f in report.findings if f.probe_id == "wilson-probe")

    expected_lo, expected_hi = wilson_ci(3, 10)
    assert f.asr == compute_asr(3, 10) == 0.3
    assert f.n_trials == 10 and f.n_success == 3
    assert f.wilson_ci == [expected_lo, expected_hi]
    assert (f.ci_low, f.ci_high) == (expected_lo, expected_hi)
    # independent numeric sanity on the exact bounds (Wilson score interval for 3/10).
    assert f.ci_low == pytest.approx(0.10779, abs=1e-4)
    assert f.ci_high == pytest.approx(0.60323, abs=1e-4)
    assert f.fired is True


def test_json_controls_are_machine_readable_and_deterministic():
    report = build_report(_run_config(), _FRAMEWORK, _CROSSWALK, _evidence_list())
    out = render_json(report)
    doc = json.loads(out)
    controls = {c["control_id"]: c for c in doc["controls"]}
    # passed control is the only auto-closeable one.
    assert controls["CPASS"]["verdict"] == "passed"
    assert controls["CPASS"]["auto_closeable"] is True
    # not_testable is serialized distinctly and is never auto-closeable (cabinet must not close it).
    assert controls["CNTBL"]["verdict"] == "not_testable"
    assert controls["CNTBL"]["auto_closeable"] is False
    # failed control lists its supporting probes with statuses.
    fail_support = {s["probe_id"]: s["status"] for s in controls["CFAILMIX"]["supporting_probes"]}
    assert fail_support == {"mix-fail": "fail", "mix-pass": "pass"}
    # deterministic: same input -> byte-identical JSON.
    assert render_json(build_report(_run_config(), _FRAMEWORK, _CROSSWALK, _evidence_list())) == out


def test_markdown_renders_verdicts_as_words():
    report = build_report(_run_config(), _FRAMEWORK, _CROSSWALK, _evidence_list())
    md = render_markdown(report)
    pass_row = next(line for line in md.splitlines() if line.startswith("| CPASS "))
    assert "PASS" in pass_row and "FAIL" not in pass_row
    fail_row = next(line for line in md.splitlines() if line.startswith("| CFAIL "))
    assert "FAIL" in fail_row
    ntbl_row = next(line for line in md.splitlines() if line.startswith("| CNTBL "))
    assert "NOT TESTABLE" in ntbl_row and "FAIL" not in ntbl_row
    # the never-reached control shows up in the untested-controls table, not as a failure.
    assert "| CNT | never reached control |" in md
