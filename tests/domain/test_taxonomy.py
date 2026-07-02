import pytest
from pydantic import ValidationError

from probe_engine.domain.base import StrictModel
from probe_engine.domain.enums import EvidenceType, TaxonomySystem
from probe_engine.domain.taxonomy import ControlOverride, TaxonomyTag


def test_taxonomy_tag_parses_atlas_id():
    tag = TaxonomyTag(system="atlas", id="AML.T0051.000", name="Direct")
    assert tag.system is TaxonomySystem.ATLAS
    assert tag.id == "AML.T0051.000"


def test_taxonomy_tag_rejects_unknown_system():
    with pytest.raises(ValidationError):
        TaxonomyTag(system="stride", id="x")


def test_taxonomy_tag_forbids_extra_fields():
    with pytest.raises(ValidationError):
        TaxonomyTag(system="cwe", id="CWE-77", bogus=1)


def test_control_override_defaults():
    ov = ControlOverride(framework="aiuc-1", control_id="B003", rationale="extraction")
    assert ov.override is True
    assert ov.evidence_type is EvidenceType.BEHAVIORAL


def test_strict_model_is_subclassable():
    class Foo(StrictModel):
        x: int

    assert Foo(x=1).x == 1
