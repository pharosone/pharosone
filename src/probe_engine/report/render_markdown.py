"""Markdown projection of a report (spec §7 sections).

The Markdown is the human-facing artifact. It is written for a non-engineer auditor: every section
opens with a one-line explanation of what it means, statuses are spelled out as words (PASS / FAIL /
NOT TESTABLE), and the honesty invariants are explicit — a control that cannot be judged from
behavior is NOT TESTABLE (never a failure) and an untested surface is a blind spot (never "robust").
"""

from probe_engine.domain.enums import CoverageStatus, EvidenceStatus
from probe_engine.report.model import ControlVerdict, Report

# Word labels for the audit verdict (no emojis — project rule; text statuses only).
_VERDICT_LABEL = {
    ControlVerdict.PASSED: "PASS",
    ControlVerdict.FAILED: "FAIL",
    ControlVerdict.UNVERIFIED: "UNVERIFIED",
    ControlVerdict.INSUFFICIENT_EVIDENCE: "INSUFFICIENT",
    ControlVerdict.PARTIAL: "PARTIAL",
    ControlVerdict.NOT_TESTED: "NOT TESTED",
    ControlVerdict.NOT_TESTABLE: "NOT TESTABLE",
}

_OVERALL_VERDICT_LABEL = {
    "fail": "FAIL — behavioral vulnerabilities were found",
    "inconclusive": "INCONCLUSIVE — no confirmed pass; unverified or under-powered controls remain",
    "pass": "PASS — the agent resisted every covered, testable control",
    "no_coverage": "NO BEHAVIORAL COVERAGE — no testable control was exercised",
}


def _evidence_status_label(ev) -> str:
    """Human-facing status cell. UNVERIFIED is rendered DISTINCTLY ('UNVERIFIED — judge required') so
    a guarded unjudged false-positive-prone verdict is never read as a confident fail/pass; every
    other status keeps its plain value (with the early-stop suffix where applicable)."""
    if ev.status is EvidenceStatus.UNVERIFIED:
        return "UNVERIFIED — judge required"
    return ev.status.value + (" (early-stop)" if ev.early_stopped else "")


def _fmt_pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.2%}"


def _fmt_taxonomy_version(scope: dict) -> str:
    tv = scope.get("taxonomy_version") or {}
    if not isinstance(tv, dict) or not tv:
        return "-"
    return ", ".join(f"{k} {tv[k]}" for k in sorted(tv))


def _truncate_ids(ids: list[str], limit: int = 5) -> str:
    if not ids:
        return "-"
    if len(ids) <= limit:
        return ", ".join(ids)
    return ", ".join(ids[:limit]) + f" (+{len(ids) - limit} more)"


def _executive_summary(report: Report) -> list[str]:
    scope = report.scope
    a = report.aggregates
    rollup = a.get("by_control_verdict", {})
    n_controls = a.get("n_controls", len(report.coverage))
    n_not_testable = a.get("n_not_testable", 0)
    n_testable = n_controls - n_not_testable
    overall = a.get("overall_verdict", "no_coverage")

    lines = ["## Executive summary", ""]
    lines.append(
        "The PharosOne Probe Engine is a behavioral vulnerability scanner for AI agents. Each probe "
        "is an attack replayed many times against the target; the results are mapped onto the "
        "**AIUC-1** control standard through the ATLAS / OWASP-Agentic / CWE crosswalk. A control is "
        "marked **PASS** only when enough distinct probes exercised it and the agent resisted every "
        "one; a single successful attack marks it **FAIL**. Controls that cannot be judged from "
        "behavior are marked **NOT TESTABLE** (never a failure) and must be closed with "
        "configuration, documentation, or telemetry evidence."
    )
    lines.append("")
    lines.append(f"- **Target**: {scope.get('target')} (tier: {scope.get('tier')})")
    lines.append(f"- **Industry**: {scope.get('industry')}")
    lines.append(f"- **Standard**: {scope.get('standard')}")
    lines.append(f"- **Taxonomy versions**: {_fmt_taxonomy_version(scope)}")
    lines.append(f"- **Corpus version**: {scope.get('corpus_version')}")
    lines.append(f"- **Run id**: {scope.get('run_id')}")
    lines.append(f"- **Timestamp**: {scope.get('timestamp')}")
    lines.append(f"- **Probes evaluated**: {a.get('n_probes', 0)}")
    if report.blind_spots:
        lines.append(f"- **Probes skipped (blind spots)**: {len(report.blind_spots)}")
    lines.append(f"- **Findings fired (attack succeeded ≥ once)**: {a.get('n_findings_fired', 0)}")
    lines.append(f"- **Overall attack success rate (ASR)**: {_fmt_pct(a.get('overall_asr'))}")
    lines.append(f"- **Overall verdict**: {_OVERALL_VERDICT_LABEL.get(overall, overall)}")
    lines.append("")
    lines.append("### AIUC-1 coverage at a glance")
    lines.append("")
    lines.append(f"- **Controls in standard**: {n_controls}")
    lines.append(
        f"- **Behaviorally testable**: {n_testable} (not testable: {n_not_testable})"
    )
    lines.append(
        "- **PASS**: {passed} | **FAIL**: {failed} | **UNVERIFIED**: {unverified} "
        "| **PARTIAL**: {partial} | **INSUFFICIENT**: {insufficient_evidence} "
        "| **NOT TESTED**: {not_tested}".format(
            passed=rollup.get("passed", 0),
            failed=rollup.get("failed", 0),
            unverified=rollup.get("unverified", 0),
            partial=rollup.get("partial", 0),
            insufficient_evidence=rollup.get("insufficient_evidence", 0),
            not_tested=rollup.get("not_tested", 0),
        )
    )
    lines.append(
        f"- **Auto-closeable in the cabinet (PASS)**: {rollup.get('passed', 0)} — every other "
        "control stays open and needs further evidence."
    )
    lines.append("")
    return lines


def _coverage_block(report: Report) -> list[str]:
    lines = ["## AIUC-1 control coverage", ""]
    lines.append(
        "One row per AIUC-1 control. **Verdict** is the audit outcome (did the agent resist?); "
        "**Coverage** is the density dimension (was it exercised by enough distinct probes?). "
        "NOT TESTABLE controls are shown but never counted as failures."
    )
    lines.append("")
    lines.append("| Control | Name | Verdict | Coverage | Probes / min | Max ASR | Fed by |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for c in report.controls:
        required = c.required_min if c.required_min is not None else "-"
        asr = _fmt_pct(c.aggregate_asr)
        note = ""
        if c.verdict is ControlVerdict.NOT_TESTABLE:
            note = " _(requires non-behavioral evidence: config/documentation/telemetry)_"
        fed_by = _truncate_ids([s.probe_id for s in c.supporting_probes])
        lines.append(
            f"| {c.control_id} | {c.title}{note} | {_VERDICT_LABEL[c.verdict]} "
            f"| {c.coverage_status.value} | {c.n_distinct_probes}/{required} | {asr} | {fed_by} |"
        )
    lines.append("")
    return lines


def _findings_block(report: Report) -> list[str]:
    lines = ["## Findings", ""]
    lines.append(
        "One row per evaluated probe (worst severity first). **ASR** is the attack success rate over "
        "**Trials**; **Wilson CI** is its 95% confidence interval. **AIUC-1** lists the controls this "
        "finding maps to; **Taxonomy** lists the ATLAS/OWASP/CWE coordinates it carries."
    )
    lines.append("")
    if not report.findings:
        lines.append("_No probes were evaluated in this run._")
        lines.append("")
        return lines
    lines.append(
        "| Probe | Severity | Scenario | Trials | ASR | Wilson CI | Verdict | AIUC-1 | Taxonomy | Source |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for f in report.findings:
        ci = f"[{f.ci_low:.2%}, {f.ci_high:.2%}]"
        trials = f"{f.n_trials}" + (f" (+{f.n_errors} err)" if f.n_errors else "")
        status = _evidence_status_label(f)
        controls = ", ".join(f.mapped_controls) if f.mapped_controls else "-"
        taxonomy = ", ".join(f"{t['system']}:{t['id']}" for t in f.taxonomy) if f.taxonomy else "-"
        lines.append(
            f"| {f.probe_id} | {f.severity.value} | {f.scenario} | {trials} | {f.asr:.2%} "
            f"| {ci} | {status} | {controls} | {taxonomy} | {f.source} |"
        )
    lines.append("")
    return lines


def _blind_spots_block(report: Report) -> list[str]:
    """What behavioral testing could NOT certify — surfaced honestly, never counted as robust."""
    lines = ["## Blind spots and untested surfaces", ""]
    lines.append(
        "Behavioral testing cannot certify what it did not exercise. The items below are explicitly "
        "**not** counted as robust — they stay open and require other evidence."
    )
    lines.append("")

    lines.append("### Skipped probes (not adjudicable on this target)")
    lines.append("")
    if report.blind_spots:
        # B2: skipped probes whose oracle the target can't adjudicate — surfaced, never a silent pass.
        for pid in report.blind_spots:
            lines.append(f"- {pid}")
    else:
        lines.append("_None — every selected probe could be adjudicated on this target._")
    lines.append("")

    not_tested = [c for c in report.controls if c.verdict is ControlVerdict.NOT_TESTED]
    lines.append("### Untested controls (behaviorally testable, no probe reached them)")
    lines.append("")
    if not_tested:
        lines.append("| Control | Name |")
        lines.append("| --- | --- |")
        for c in not_tested:
            lines.append(f"| {c.control_id} | {c.title} |")
    else:
        lines.append("_None — every behaviorally testable control was exercised._")
    lines.append("")

    not_testable = [c for c in report.controls if c.verdict is ControlVerdict.NOT_TESTABLE]
    lines.append("### Not-testable controls (need configuration / documentation / telemetry)")
    lines.append("")
    if not_testable:
        lines.append("| Control | Name |")
        lines.append("| --- | --- |")
        for c in not_testable:
            lines.append(f"| {c.control_id} | {c.title} |")
    else:
        lines.append("_None._")
    lines.append("")
    return lines


def _plan_block(report: Report) -> list[str]:
    """The trial-allocation plan: strategy + planner model + per-probe variants/epochs (audit
    reproducibility). Empty when no plan was recorded, so a planner-free report is unchanged."""
    plan = report.plan
    if not plan:
        return []
    lines = ["## Run plan", ""]
    lines.append(f"- **strategy**: {plan.get('strategy')}")
    lines.append(f"- **model**: {plan.get('model')}")
    lines.append(f"- **total trials**: {plan.get('total_trials')}")
    notes = plan.get("notes")
    if notes:
        lines.append(f"- **notes**: {notes}")
    lines.append("")
    items = plan.get("items") or []
    if items:
        lines.append("| Probe | Variants | Epochs | Priority | Rationale |")
        lines.append("| --- | --- | --- | --- | --- |")
        for it in items:
            lines.append(
                f"| {it.get('probe_id')} | {it.get('n_variants')} | {it.get('epochs')} "
                f"| {it.get('priority')} | {it.get('rationale', '')} |"
            )
        lines.append("")
    return lines


def _synthesis_block(report: Report) -> list[str]:
    """The LLM-synthesis outcome: accepted probe ids + the rejected triage queue (count + reasons).
    The oracle still decides success — these are PROPOSALS recorded for audit. Empty when no
    synthesis was recorded, so a synthesis-free report is unchanged."""
    synth = report.synthesis
    if not synth:
        return []
    lines = ["## Synthesized probes", ""]
    lines.append(f"- **model**: {synth.get('model')}")
    notes = synth.get("notes")
    if notes:
        lines.append(f"- **notes**: {notes}")
    accepted = synth.get("accepted_ids") or []
    rejected = synth.get("rejected") or []
    lines.append(f"- **accepted** ({len(accepted)}): {', '.join(accepted) if accepted else '—'}")
    lines.append(f"- **rejected**: {len(rejected)}")
    lines.append("")
    if rejected:
        lines.append("| Rejected candidate | Reasons |")
        lines.append("| --- | --- |")
        for r in rejected:
            raw = r.get("raw") or {}
            cid = raw.get("id") or raw.get("title") or "?"
            reasons = "; ".join(r.get("reasons") or [])
            lines.append(f"| {cid} | {reasons} |")
        lines.append("")
    return lines


def render_markdown(report: Report) -> str:
    parts: list[str] = ["# PharosOne Probe Engine Report", ""]
    parts += _executive_summary(report)
    parts += _coverage_block(report)
    parts += _findings_block(report)
    parts += _blind_spots_block(report)
    parts += _plan_block(report)
    parts += _synthesis_block(report)
    return "\n".join(parts)
