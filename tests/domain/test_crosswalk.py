from probe_engine.domain.crosswalk import Crosswalk, CrosswalkControlRef, Mapping
from probe_engine.domain.enums import EvidenceType, TaxonomySystem


def _crosswalk() -> Crosswalk:
    return Crosswalk(
        framework="aiuc-1",
        framework_version="v1",
        taxonomy_version={"atlas": "v5.4.0", "owasp_agentic": "2025-12"},
        entries=[
            Mapping(
                taxonomy_system="atlas",
                taxonomy_id="AML.T0051.001",
                controls=[
                    CrosswalkControlRef(control_id="B001"),
                    CrosswalkControlRef(control_id="B005"),
                ],
            ),
            Mapping(
                taxonomy_system="owasp_agentic",
                taxonomy_id="ASI02",
                controls=[CrosswalkControlRef(control_id="B006")],
            ),
        ],
    )


def test_lookup_returns_controls_for_exact_coordinate():
    cw = _crosswalk()
    refs = cw.lookup(TaxonomySystem.ATLAS, "AML.T0051.001")
    assert {r.control_id for r in refs} == {"B001", "B005"}


def test_lookup_missing_returns_empty():
    assert _crosswalk().lookup(TaxonomySystem.CWE, "CWE-77") == []


def test_lookup_is_exact_not_prefix():
    # parent technique must not match a sub-technique entry (granularity, §5.4)
    assert _crosswalk().lookup(TaxonomySystem.ATLAS, "AML.T0051") == []


def test_control_ref_default_evidence_type():
    assert CrosswalkControlRef(control_id="B001").evidence_type is EvidenceType.BEHAVIORAL
