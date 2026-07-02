from pathlib import Path

from probe_engine.corpus.loader import load_corpus
from probe_engine.domain.enums import CoverageStatus
from probe_engine.domain.run import Principal, RunConfig, TargetConfig, Thresholds
from probe_engine.mapping.loader import load_crosswalk, load_framework
from probe_engine.report.builder import build_report
from probe_engine.report.render_markdown import render_markdown
from probe_engine.run.executor import run_corpus
from probe_engine.targets.mock import MockPolicy

ROOT = Path(__file__).parents[2]


def _run(tmp_path, rule: str, threshold: int = 30):
    probes = load_corpus(ROOT / "corpus" / "probes")
    fw = load_framework(ROOT / "frameworks" / "aiuc-1.yaml")
    cw = load_crosswalk(ROOT / "crosswalks" / "aiuc-1" / "crosswalk.yaml")
    # seed a VULNERABLE authz backend (acting principal + a cross-owner resource + allow) so the
    # authz_violation probe also "fails" under the always-vulnerable scenario (enforcement axis):
    # without a permissive backend a cross-owner attempt is correctly robust, so the full-corpus
    # "every probe fails" invariant requires the backend to allow it.
    rc = RunConfig(
        target=TargetConfig(
            tier="mock",
            acting_principal=Principal(id="U_test"),
            resource_owners={"RES_VICTIM": "U_other"},
            authz_default="allow",
            # a generic lifecycle so the state-invariant probe (which runs here — run_corpus has no
            # selection gate) also "fails" under the always-vulnerable scenario (laundering fires).
            seed_stage="review",
            lifecycle_order=["rejected", "pending", "review", "approved"],
            lifecycle_floor="review",
        ),
        n_variants=4, epochs=2,
        thresholds=Thresholds(), run_id="e2e", timestamp="2026-06-22T00:00:00Z",
    )
    evidence = run_corpus(probes, rc, mock_policy=MockPolicy(rule=rule, threshold=threshold),
                          seed=7, log_dir=str(tmp_path / "logs"))
    return build_report(rc, fw, cw, evidence)


def test_full_pipeline_always_vulnerable(tmp_path):
    report = _run(tmp_path, "always")
    # every probe ran and failed -> high overall ASR (full corpus passed directly to run_corpus,
    # no select_probes gate -> all probes run regardless of channel)
    assert report.aggregates["n_probes"] == 118
    assert report.aggregates["overall_asr"] == 1.0
    # all 49 controls represented in coverage
    assert len(report.coverage) == 49
    # B008 is honestly flagged not-testable, never failed (§5.3)
    b008 = next(c for c in report.coverage if c.control_id == "B008")
    assert b008.status is CoverageStatus.NOT_TESTABLE
    # B001 is reached by >=5 distinct probes -> density met -> covered
    b001 = next(c for c in report.coverage if c.control_id == "B001")
    assert b001.n_distinct_probes >= 5
    assert b001.density_met is True
    # gaps never include a not-testable control
    assert all(g.behaviorally_testable for g in report.gaps)
    # markdown renders all sections
    md = render_markdown(report)
    for section in ["## Executive summary", "## AIUC-1 control coverage", "## Findings",
                    "## Blind spots and untested surfaces"]:
        assert section in md


def test_full_pipeline_never_vulnerable_has_gaps(tmp_path):
    report = _run(tmp_path, "never")
    assert report.aggregates["overall_asr"] == 0.0
    # most controls have no covering evidence -> gaps exist (uncovered)
    assert len(report.gaps) > 0
    # leverage: one indirect-injection evidence credits multiple controls (§5.5)
    b001 = next(c for c in report.coverage if c.control_id == "B001")
    assert b001.n_distinct_probes >= 5
