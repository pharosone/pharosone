from probe_engine.domain.crosswalk import Crosswalk, CrosswalkControlRef, Mapping
from probe_engine.domain.enums import (
    CoverageStatus,
    EvidenceStatus,
    EvidenceType,
)
from probe_engine.domain.evidence import Evidence
from probe_engine.domain.framework import (
    Control,
    DensityThreshold,
    Framework,
)
from probe_engine.domain.probe import Provenance
from probe_engine.domain.taxonomy import ControlOverride, TaxonomyTag
from probe_engine.mapping.coverage import compute_coverage, resolve_controls


def _framework() -> Framework:
    return Framework(
        id="aiuc-1",
        version="v1",
        name="AIUC-1",
        controls=[
            Control(id="B001", category="B", title="adv robustness",
                    density_threshold=DensityThreshold(min=2, max=3)),
            Control(id="B003", category="B", title="public release details"),
            Control(id="B008", category="B", title="deploy env",
                    behaviorally_testable=False),
        ],
    )


def _crosswalk() -> Crosswalk:
    return Crosswalk(
        framework="aiuc-1", framework_version="v1",
        entries=[
            Mapping(taxonomy_system="atlas", taxonomy_id="AML.T0051.001",
                    controls=[CrosswalkControlRef(control_id="B001")]),
        ],
    )


def _evidence(pid: str, asr: float, override=False) -> Evidence:
    return Evidence(
        probe_id=pid, severity="high",
        taxonomy_tags=[TaxonomyTag(system="atlas", id="AML.T0051.001")],
        control_overrides=(
            [ControlOverride(framework="aiuc-1", control_id="B003", rationale="extraction")]
            if override else []
        ),
        provenance=Provenance(source="X"),
        n_trials=10, n_success=int(asr * 10), asr=asr, status="fail",
    )


def test_resolve_controls_via_crosswalk_and_override():
    refs = resolve_controls(_evidence("p1", 0.1, override=True), _crosswalk())
    by_id = {cid: (etype, ov) for cid, etype, ov in refs}
    assert by_id["B001"] == (EvidenceType.BEHAVIORAL, False)   # via crosswalk
    assert by_id["B003"] == (EvidenceType.BEHAVIORAL, True)    # via override


def test_density_partial_then_covered():
    fw, cw = _framework(), _crosswalk()
    one = compute_coverage(fw, cw, [_evidence("p1", 0.1)])
    b001_one = next(c for c in one if c.control_id == "B001")
    assert b001_one.n_distinct_probes == 1
    assert b001_one.status is CoverageStatus.PARTIAL  # threshold min=2

    two = compute_coverage(fw, cw, [_evidence("p1", 0.1), _evidence("p2", 0.3)])
    b001_two = next(c for c in two if c.control_id == "B001")
    assert b001_two.n_distinct_probes == 2
    assert b001_two.density_met is True
    assert b001_two.status is CoverageStatus.COVERED
    assert b001_two.aggregate_asr == 0.3  # worst-case


def test_non_testable_control_flagged_not_failed():
    cov = compute_coverage(_framework(), _crosswalk(), [])
    b008 = next(c for c in cov if c.control_id == "B008")
    assert b008.status is CoverageStatus.NOT_TESTABLE


def test_uncovered_control():
    cov = compute_coverage(_framework(), _crosswalk(), [])
    b001 = next(c for c in cov if c.control_id == "B001")
    assert b001.status is CoverageStatus.UNCOVERED
    assert b001.n_distinct_probes == 0


def test_distinct_probes_not_double_counted():
    fw, cw = _framework(), _crosswalk()
    cov = compute_coverage(fw, cw, [_evidence("p1", 0.1), _evidence("p1", 0.1)])
    b001 = next(c for c in cov if c.control_id == "B001")
    assert b001.n_distinct_probes == 1
