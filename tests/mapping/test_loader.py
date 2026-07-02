from pathlib import Path

import pytest

from probe_engine.domain.enums import TaxonomySystem
from probe_engine.mapping.loader import MappingError, load_crosswalk, load_framework

FIX = Path(__file__).parent / "fixtures"


def test_load_framework():
    fw = load_framework(FIX / "aiuc-1.yaml")
    assert fw.id == "aiuc-1"
    assert fw.get_control("B008").behaviorally_testable is False
    assert fw.get_control("B001").density_threshold.min == 5


def test_load_crosswalk_and_lookup():
    cw = load_crosswalk(FIX / "crosswalk.yaml")
    assert cw.framework_version == "v1"
    refs = cw.lookup(TaxonomySystem.ATLAS, "AML.T0051.001")
    assert {r.control_id for r in refs} == {"B001", "B005"}


def test_bad_framework_raises(tmp_path):
    (tmp_path / "f.yaml").write_text("id: x\n")  # missing required fields
    with pytest.raises(MappingError):
        load_framework(tmp_path / "f.yaml")
