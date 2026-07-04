"""Attack-approaches (scenario-family) selection filter + honest scope-exclusion surfacing.

The `approaches` knob lets the operator narrow a run to a subset of scenario families
(single_turn / chain / adaptive). Narrowing is a DELIBERATE scope reduction — the dropped probes are
surfaced via `scope_excluded` (never a blind spot, never a silent pass). Default = all three, which
is byte-identical to the pre-`approaches` behavior."""

import pytest
from pydantic import ValidationError

from probe_engine.domain.probe import (
    Applicability,
    BinaryCheck,
    Evaluation,
    Probe,
    Provenance,
    Scenario,
    Turn,
)
from probe_engine.domain.run import RunConfig, TargetConfig, Thresholds
from probe_engine.run.selection import scope_excluded, select_probes


def _probe(pid, scenario_type, tools=None):
    return Probe(
        id=pid, title="t", severity="high", intent="x",
        taxonomy_tags=[{"system": "atlas", "id": "AML.T0051.001"}],
        applicability=Applicability(industries=["any"], required_tools=tools or []),
        scenario=Scenario(type=scenario_type, turns=[Turn(role="user", seed_prompts=["hi"])]),
        evaluation=Evaluation(binary=BinaryCheck(oracle="tool_called", args={"tool": "x"})),
        provenance=Provenance(source="X"),
    )


def _rc(approaches=None, tools=None):
    kw = {} if approaches is None else {"approaches": approaches}
    return RunConfig(
        target=TargetConfig(tier="mock"),
        industry="any",
        available_tools=tools or [],
        thresholds=Thresholds(),
        run_id="r",
        timestamp="t",
        **kw,
    )


_ALL = [_probe("s", "single_turn"), _probe("c", "chain"), _probe("a", "adaptive")]


def test_default_runs_every_approach():
    """Omitting `approaches` = all three families select (backward compatible)."""
    assert {p.id for p in select_probes(_ALL, _rc())} == {"s", "c", "a"}
    assert scope_excluded(_ALL, _rc()) == []


def test_single_family_selects_only_that_family():
    got = {p.id for p in select_probes(_ALL, _rc(approaches=["single_turn"]))}
    assert got == {"s"}


def test_two_families_select_and_third_is_scope_excluded():
    rc = _rc(approaches=["single_turn", "chain"])
    assert {p.id for p in select_probes(_ALL, rc)} == {"s", "c"}
    assert {p.id for p in scope_excluded(_ALL, rc)} == {"a"}


def test_scope_excluded_ignores_probes_that_fail_for_other_reasons():
    """A chain probe requiring a tool the agent lacks is NOT scope-excluded — it never applied at all,
    so it's a genuine non-applicability, not a deliberate approach de-selection."""
    probes = [
        _probe("s", "single_turn"),
        _probe("c_needs_tool", "chain", tools=["move_file"]),  # tool absent -> doesn't apply anyway
    ]
    rc = _rc(approaches=["single_turn"], tools=[])  # no tools declared -> tool filter inactive
    # With no declared inventory the tool filter is skipped, so c_needs_tool WOULD apply except scope.
    assert {p.id for p in scope_excluded(probes, rc)} == {"c_needs_tool"}
    # But once the agent declares a (different) tool inventory, the chain probe fails the tool gate and
    # is NOT counted as a scope exclusion.
    rc2 = _rc(approaches=["single_turn"], tools=["send_message"])
    assert scope_excluded(probes, rc2) == []


def test_selected_and_scope_excluded_are_disjoint():
    rc = _rc(approaches=["adaptive"])
    sel = {p.id for p in select_probes(_ALL, rc)}
    exc = {p.id for p in scope_excluded(_ALL, rc)}
    assert sel == {"a"}
    assert sel.isdisjoint(exc)


def test_unknown_approach_rejected():
    with pytest.raises(ValidationError):
        _rc(approaches=["single_turn", "telepathy"])


def test_empty_approaches_rejected():
    with pytest.raises(ValidationError):
        _rc(approaches=[])
