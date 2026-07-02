from probe_engine.domain.coverage import ControlContribution, Coverage
from probe_engine.domain.enums import CoverageStatus, EvidenceStatus, EvidenceType
from probe_engine.domain.framework import DensityThreshold
from probe_engine.domain.run import RunConfig, TargetConfig, Thresholds


def test_coverage_holds_contributions():
    cov = Coverage(
        control_id="B001",
        framework="aiuc-1",
        category="B",
        title="Third-party testing of adversarial robustness",
        behaviorally_testable=True,
        density_threshold=DensityThreshold(min=5, max=7),
        n_distinct_probes=2,
        density_met=False,
        aggregate_asr=0.1,
        status=CoverageStatus.PARTIAL,
        evidence_types=[EvidenceType.BEHAVIORAL],
        contributions=[
            ControlContribution(
                probe_id="p1",
                asr=0.1,
                status=EvidenceStatus.FAIL,
                evidence_type=EvidenceType.BEHAVIORAL,
            )
        ],
    )
    assert cov.status is CoverageStatus.PARTIAL
    assert cov.contributions[0].via_override is False


def test_runconfig_defaults():
    rc = RunConfig(
        target=TargetConfig(),
        thresholds=Thresholds(),
        run_id="run-1",
        timestamp="2026-06-22T00:00:00Z",
    )
    assert rc.target.tier == "mock"
    assert rc.standards == ["aiuc-1"]
    assert rc.thresholds.target_asr == 0.01
    assert rc.epochs == 1
