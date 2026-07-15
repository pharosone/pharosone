"""JSON projection of a report — the machine-readable artifact the PharosOne cabinet ingests.

The cabinet reads `report.json` to AUTO-CLOSE covered AIUC-1 controls. The stable, deterministic
contract it relies on:

* ``scope`` — run metadata (target, tier, industry, standard + framework_id/version, taxonomy
  versions, corpus version, run id, timestamp, thresholds).
* ``controls`` — one record per AIUC-1 control with ``control_id``, ``verdict``
  (passed/failed/unverified/insufficient_evidence/partial/not_tested/not_testable), ``auto_closeable``
  (True ONLY for a passed control), the ``supporting_probes`` that close/fail it, and aggregate
  metrics. ``not_testable`` MUST NOT be read as pass or fail.
* ``findings`` — one stats-only record per probe with ASR + Wilson CI + trial counts + status +
  the resolved ``mapped_controls`` and ``taxonomy`` coordinates.
* ``aggregates`` — rollups incl. ``by_control_verdict`` and ``overall_verdict``.
* ``coverage`` / ``gaps`` — the density dimension (retained, back-compatible).
* ``blind_spots`` — probes skipped at run time (present only when non-empty).
* ``errored_probes`` — probes that errored out at run time, all samples failed (present only when non-empty).

Serialization is deterministic: field order is fixed by the models and every list is sorted
(controls in framework order, findings by severity then id, supporting_probes / mapped_controls /
taxonomy sorted). No transcript, protected reference, or oracle patterns ever enter this artifact —
``findings`` and ``controls`` are stats-only by construction.
"""

import json

from probe_engine.domain.enums import EvidenceStatus
from probe_engine.report.model import Report

# Human-facing label injected next to an UNVERIFIED status so a JSON consumer renders it DISTINCTLY
# (never as fail/pass). The machine-readable `status` field stays the plain enum value "unverified".
UNVERIFIED_STATUS_LABEL = "UNVERIFIED — judge required"


def render_json(report: Report) -> str:
    # `plan`/`synthesis` are optional audit blocks added later; drop them from the JSON entirely
    # when absent so a report built WITHOUT a planner/synthesis is byte-compatible with before
    # (every other field — including pre-existing nullable ones — is serialized unchanged).
    exclude = {
        k for k in ("plan", "synthesis") if getattr(report, k) is None
    }
    # B2: blind_spots defaults to [] — drop it when empty so a report with no skipped probes stays
    # byte-compatible with before (mirrors the plan/synthesis handling above).
    if not report.blind_spots:
        exclude.add("blind_spots")
    # errored_probes defaults to [] — drop it when empty so a clean run stays byte-compatible.
    if not report.errored_probes:
        exclude.add("errored_probes")
    # Scope exclusions (deliberately de-selected approaches) default to [] — drop when empty so an
    # unnarrowed run is byte-compatible with before this field existed (same rule as blind_spots).
    if not report.excluded_approaches:
        exclude.add("excluded_approaches")
    if not report.scope_excluded_probes:
        exclude.add("scope_excluded_probes")
    raw = report.model_dump_json(indent=2, exclude=exclude or None)

    # GUARD: annotate every UNVERIFIED evidence entry with a distinct human-facing `status_label`
    # ('UNVERIFIED — judge required') so a renderer never shows it as fail/pass. A report with NO
    # unverified evidence is left byte-for-byte unchanged (the common case — no re-serialization).
    if not any(ev.status is EvidenceStatus.UNVERIFIED for ev in report.evidence):
        return raw
    obj = json.loads(raw)
    for ev in obj.get("evidence", []):
        if ev.get("status") == EvidenceStatus.UNVERIFIED.value:
            ev["status_label"] = UNVERIFIED_STATUS_LABEL
    return json.dumps(obj, indent=2)
