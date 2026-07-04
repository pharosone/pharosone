# Approaches filter + guided depth/approaches intake — design

**Date:** 2026-07-03
**Status:** approved (user authorized end-to-end implementation)

## Problem

The pharosone onboarding must ALWAYS guide the operator through choice-based questions before a
certification run:

1. **Which attack approaches** to include (single-turn / chains / adaptive), with each option's
   nuance + relative cost/time, multi-select.
2. **Target ASR** the run should be powered to detect (`≤10 / ≤5 / ≤3 / ≤2%` + custom), explaining
   what the bar means AND the concrete number of attacks it implies.
3. The whole flow stays a guided onboarding on every entry path (router + sub-skills), never a
   silent default.

Today attack style is a per-probe `scenario.type` (69 single_turn / 37 chain / 12 adaptive of 118);
there is **no run-level knob** to include/exclude approaches, so asking about it would be theater.

## Decisions (user-selected)

- **Scope:** add a real engine knob (not skill-text only).
- **Target meaning:** target ASR % robustness bar; each option shows trials/probe and the concrete
  total attack count (`selected N × trials/probe`).
- **Depth ladder:** `≤10 / ≤5 / ≤3 / ≤2%` (36 / 75 / 126 / 189 trials-per-probe) + custom.
- **Honesty:** end-to-end — excluded approaches surface in CLI/web output AND the saved
  report/coverage AND the validate-and-certify scorecard as "not tested (scope)", never robust.
- **Enforcement:** router + sub-skills.

## Engine changes

- `domain/run.py::RunConfig.approaches: list[str]` — default all three scenario types (byte-for-byte
  backward compatible). Validated against `ScenarioType`; non-empty.
- `run/selection.py` — `probe_applies` drops a probe whose `scenario.type` is not in `approaches`.
  New `scope_excluded(probes, run_config)` returns probes that would apply EXCEPT for the approaches
  filter (a deliberate scope reduction — never a blind spot, never a pass).
- `config/profile.py::RunProfile.approaches` threaded into `run_config_from_profile`.
- `cli.py run` — `--approaches single_turn,chain,adaptive`; validates; prints a scope-excluded line;
  passes `scope_excluded` to `build_report`.
- `web/app.py` — `RunRequest.approaches`; applied to run_config; excluded passed to report.
- `report/builder.py` + `report/model.py` — `Report.excluded_approaches` + `scope_excluded_probes`;
  `scope["approaches"]`; `build_report(..., scope_excluded=...)`.
- `report/render_markdown.py` — "Approaches not tested (scope choice)" subsection + exec-summary line.
- `templates/profile_template.yaml` — `approaches:` field with comment.

Depth ladder trials/probe are the same Wilson-upper-bound-at-0 rule the existing rows use
(`z²/(n+z²) < target`, z=1.96): ≤10%→36 (12×3), ≤5%→75 (25×3), ≤3%→126 (42×3), ≤2%→189 (63×3).

## Skill changes

- `pharosone/SKILL.md` — round A gains a multiSelect **Approaches** question (de-conflated from
  Techniques, which keeps only paraphrase-breadth + synthesis); **Depth** reframed to the ladder +
  custom with trials/probe; **0.6 run contract** computes the exact attack count and lists excluded
  approaches; always-ask invariant names approaches + target ASR.
- `build-run-profile/SKILL.md` — `approaches` in checklist + profile output; depth→attack-count
  table; no-silent-default note.
- `validate-and-certify/SKILL.md` — excluded approaches in the scorecard as scope items.

## Tests

Selection filter (each subset + default-all back-compat), profile round-trip of `approaches`,
CLI `--approaches` parse/validate, `scope_excluded` reporting, report field + markdown note.
