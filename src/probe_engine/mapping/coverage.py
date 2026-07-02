"""Coverage engine: Evidence -> taxonomy -> crosswalk -> controls, with density (spec §5)."""

from probe_engine.domain.coverage import ControlContribution, Coverage
from probe_engine.domain.crosswalk import Crosswalk
from probe_engine.domain.enums import CoverageStatus, EvidenceType
from probe_engine.domain.evidence import Evidence
from probe_engine.domain.framework import Framework


def resolve_controls(
    evidence: Evidence, crosswalk: Crosswalk
) -> list[tuple[str, EvidenceType, bool]]:
    """Controls reached by this evidence: (control_id, evidence_type, via_override)."""
    out: dict[str, tuple[EvidenceType, bool]] = {}
    for tag in evidence.taxonomy_tags:
        for ref in crosswalk.lookup(tag.system, tag.id):
            # crosswalk wins on evidence_type; never downgrade a behavioral mapping
            if ref.control_id not in out:
                out[ref.control_id] = (ref.evidence_type, False)
    for ov in evidence.control_overrides:
        if ov.framework == crosswalk.framework:
            out.setdefault(ov.control_id, (ov.evidence_type, True))
    return [(cid, etype, ov) for cid, (etype, ov) in out.items()]


def compute_coverage(
    framework: Framework, crosswalk: Crosswalk, evidence_list: list[Evidence]
) -> list[Coverage]:
    # control_id -> {probe_id -> ControlContribution}
    hits: dict[str, dict[str, ControlContribution]] = {
        c.id: {} for c in framework.controls
    }
    for ev in evidence_list:
        for control_id, etype, via_override in resolve_controls(ev, crosswalk):
            if control_id not in hits:
                continue  # crosswalk references a control absent from this framework version
            hits[control_id][ev.probe_id] = ControlContribution(
                probe_id=ev.probe_id,
                asr=ev.asr,
                status=ev.status,
                evidence_type=etype,
                via_override=via_override,
            )

    coverages: list[Coverage] = []
    for control in framework.controls:
        contribs = list(hits[control.id].values())
        n = len(contribs)
        threshold = control.density_threshold
        if threshold is None:
            density_met = n >= 1
        else:
            density_met = n >= threshold.min

        if not control.behaviorally_testable:
            status = CoverageStatus.NOT_TESTABLE
        elif n == 0:
            status = CoverageStatus.UNCOVERED
        elif density_met:
            status = CoverageStatus.COVERED
        else:
            status = CoverageStatus.PARTIAL

        coverages.append(
            Coverage(
                control_id=control.id,
                framework=framework.id,
                category=control.category,
                title=control.title,
                behaviorally_testable=control.behaviorally_testable,
                density_threshold=threshold,
                n_distinct_probes=n,
                density_met=density_met,
                aggregate_asr=(max(c.asr for c in contribs) if contribs else None),
                status=status,
                evidence_types=sorted({c.evidence_type for c in contribs}, key=lambda e: e.value),
                contributions=contribs,
            )
        )
    return coverages
