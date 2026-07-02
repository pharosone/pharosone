from pathlib import Path

import pytest

from probe_engine.corpus.loader import load_corpus
from probe_engine.domain.evidence import Evidence
from probe_engine.mapping.coverage import resolve_controls
from probe_engine.mapping.loader import load_crosswalk, load_framework

ROOT = Path(__file__).parents[2]


@pytest.fixture(scope="module")
def seed():
    probes = load_corpus(ROOT / "corpus" / "probes")
    fw = load_framework(ROOT / "frameworks" / "aiuc-1.yaml")
    cw = load_crosswalk(ROOT / "crosswalks" / "aiuc-1" / "crosswalk.yaml")
    return probes, fw, cw


def test_seed_counts(seed):
    probes, fw, cw = seed
    assert len(probes) == 118
    assert len(fw.controls) == 49
    assert len(cw.entries) >= 20


def test_crosswalk_controls_exist_in_framework(seed):
    _, fw, cw = seed
    fw_ids = fw.control_ids()
    referenced = {r.control_id for e in cw.entries for r in e.controls}
    assert referenced <= fw_ids, f"crosswalk references unknown controls: {referenced - fw_ids}"


def test_every_probe_resolves_to_a_control(seed):
    probes, _, cw = seed
    for p in probes:
        ev = Evidence(probe_id=p.id, severity=p.severity, taxonomy_tags=p.taxonomy_tags,
                      control_overrides=p.control_overrides, provenance=p.provenance)
        controls = resolve_controls(ev, cw)
        assert controls, f"probe {p.id} resolves to no control"


def test_every_declared_taxonomy_coordinate_is_crosswalked(seed):
    # A probe must not declare a taxonomy coordinate that the crosswalk does not map: such a tag
    # would silently contribute no coverage (the probe still resolves via its OTHER tags, hiding
    # the gap). Guards against future false coverage — every (system, id) a probe cites is mapped.
    probes, _, cw = seed
    have = {(e.taxonomy_system, e.taxonomy_id) for e in cw.entries}
    missing = {
        (t.system, t.id, p.id)
        for p in probes
        for t in (p.taxonomy_tags or [])
        if (t.system, t.id) not in have
    }
    assert not missing, f"probes declare taxonomy coordinates absent from the crosswalk: {sorted(missing)}"


def test_extraction_probe_uses_both_crosswalk_and_override(seed):
    probes, _, cw = seed
    extraction = next(p for p in probes if p.id == "extraction-system-prompt")
    ev = Evidence(probe_id=extraction.id, severity=extraction.severity,
                  taxonomy_tags=extraction.taxonomy_tags,
                  control_overrides=extraction.control_overrides,
                  provenance=extraction.provenance)
    by_id = {cid: via_override for cid, _etype, via_override in resolve_controls(ev, cw)}
    assert by_id.get("B001") is False   # via crosswalk (AML.T0051.000)
    assert by_id.get("B003") is True    # via override (§5.4)
    assert by_id.get("B009") is True    # via override


def test_b008_is_not_behaviorally_testable(seed):
    _, fw, _ = seed
    assert fw.get_control("B008").behaviorally_testable is False
