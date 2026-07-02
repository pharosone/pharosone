from probe_engine.domain.enums import EvidenceStatus, Severity
from probe_engine.domain.evidence import Evidence, Trial, Variant
from probe_engine.domain.probe import Provenance
from probe_engine.domain.taxonomy import TaxonomyTag


def test_evidence_minimal_defaults():
    ev = Evidence(
        probe_id="p1",
        severity="high",
        taxonomy_tags=[TaxonomyTag(system="atlas", id="AML.T0051.001")],
        provenance=Provenance(source="AgentDyn"),
    )
    assert ev.status is EvidenceStatus.NOT_RUN
    assert ev.severity is Severity.HIGH
    assert ev.control_overrides == []


def test_evidence_holds_trials_and_stats():
    ev = Evidence(
        probe_id="p1",
        severity="low",
        taxonomy_tags=[],
        provenance=Provenance(source="X"),
        n_trials=10,
        n_success=2,
        asr=0.2,
        ci_low=0.05,
        ci_high=0.52,
        status="fail",
        trials=[Trial(variant_id="v1", epoch=0, success=True)],
    )
    assert ev.n_success == 2
    assert ev.trials[0].success is True


def test_variant_defaults():
    v = Variant(probe_id="p1", variant_id="p1#0")
    assert v.language == "en"
    assert v.mutation_seed == 0
