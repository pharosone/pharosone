"""Inventory-alignment tests: the library_spec must mirror the oracle/mock/domain code so the
synthesizer can only ever propose scoreable, fireable probes (network-free, pure)."""

import json
from pathlib import Path

from probe_engine.domain.enums import ScenarioType
from probe_engine.domain.probe import CANONICAL_CHANNELS
from probe_engine.plan import library_spec as ls

_ROOT = Path(__file__).resolve().parents[2]
_CROSSWALK = str(_ROOT / "crosswalks" / "aiuc-1" / "crosswalk.yaml")

# The headline oracle kinds the spec explicitly calls out — these MUST be present.
_EXPECTED_KINDS = {
    "tool_called",
    "arg_injection",
    "output_pattern",
    "cross_turn_contradiction",
    "runaway_output",
    "secret_fragment",
    "prompt_leak",
    "contains",
}


# ---- ORACLE_KINDS / MOCK_FIREABLE_ORACLES ------------------------------------------------


def test_oracle_kinds_non_empty_and_include_expected():
    assert ls.ORACLE_KINDS
    assert _EXPECTED_KINDS <= ls.ORACLE_KINDS


def test_mock_fireable_oracles_non_empty_and_include_expected():
    assert ls.MOCK_FIREABLE_ORACLES
    assert _EXPECTED_KINDS <= ls.MOCK_FIREABLE_ORACLES


def test_mock_fireable_is_subset_of_oracle_kinds():
    # A probe may only be synthesized for an oracle that BOTH the scorer decides and the mock can
    # fire — fireable can never advertise a kind the oracle does not handle.
    assert ls.MOCK_FIREABLE_ORACLES <= ls.ORACLE_KINDS


def test_oracle_kinds_matches_evaluate_oracle_source():
    # Pin alignment to the actual evaluate_oracle implementation: every `kind == "..."` literal in
    # the source must appear in ORACLE_KINDS (drift in the oracle is caught here).
    import re

    import probe_engine.scoring.oracle as oracle_mod

    src = Path(oracle_mod.__file__).read_text(encoding="utf-8")
    in_source = set(re.findall(r'kind == "([a-z_]+)"', src))
    assert in_source, "no oracle kinds parsed from evaluate_oracle source"
    assert in_source == set(ls.ORACLE_KINDS), (
        f"ORACLE_KINDS drift: source={sorted(in_source)} spec={sorted(ls.ORACLE_KINDS)}"
    )


def test_mock_fireable_matches_apply_outcome_source():
    # Every `binary.oracle == "..."` branch in mock._apply_outcome must be in MOCK_FIREABLE_ORACLES.
    import re

    import probe_engine.targets.mock as mock_mod

    src = Path(mock_mod.__file__).read_text(encoding="utf-8")
    in_source = set(re.findall(r'binary\.oracle == "([a-z_]+)"', src))
    assert in_source, "no oracle branches parsed from mock._apply_outcome source"
    assert in_source == set(ls.MOCK_FIREABLE_ORACLES), (
        f"MOCK_FIREABLE_ORACLES drift: source={sorted(in_source)} spec={sorted(ls.MOCK_FIREABLE_ORACLES)}"
    )


# ---- channels / scenarios / capabilities -------------------------------------------------


def test_known_channels_equals_canonical_channels():
    assert ls.known_channels() == set(CANONICAL_CHANNELS)


def test_known_scenarios_matches_scenario_type():
    assert ls.known_scenarios() == {s.value for s in ScenarioType}
    assert "single_turn" in ls.known_scenarios()
    assert "chain" in ls.known_scenarios()


def test_capability_vocab_non_empty_and_has_core_caps():
    caps = ls.capability_vocab()
    assert caps
    for c in ("send_message", "transfer", "read_file", "pii_lookup", "account_modify"):
        assert c in caps


# ---- crosswalk taxonomy ids --------------------------------------------------------------


def test_crosswalk_taxonomy_ids_non_empty_and_includes_known():
    ids = ls.crosswalk_taxonomy_ids(_CROSSWALK)
    assert ids
    assert "ASI01" in ids
    assert "AML.T0051" in ids


# ---- library_menu ------------------------------------------------------------------------


def test_library_menu_shape_and_serializable():
    menu = ls.library_menu(_CROSSWALK)
    # JSON-serializable (it is handed to the synthesis LLM)
    assert json.loads(json.dumps(menu)) == menu
    assert set(menu.keys()) == {
        "oracle_kinds",
        "channels",
        "scenarios",
        "capabilities",
        "taxonomy_ids",
    }


def test_library_menu_oracle_kinds_carry_arg_hints():
    menu = ls.library_menu(_CROSSWALK)
    advertised = {entry["oracle"] for entry in menu["oracle_kinds"]}
    # the menu advertises EXACTLY the fireable kinds — never a kind the mock cannot fire
    assert advertised == set(ls.MOCK_FIREABLE_ORACLES)
    for entry in menu["oracle_kinds"]:
        assert "arg_hint" in entry
        assert isinstance(entry["arg_hint"], str)
        assert entry["arg_hint"], f"missing arg hint for {entry['oracle']}"


def test_library_menu_channels_scenarios_caps_taxonomy_match_helpers():
    menu = ls.library_menu(_CROSSWALK)
    assert set(menu["channels"]) == ls.known_channels()
    assert set(menu["scenarios"]) == ls.known_scenarios()
    assert set(menu["capabilities"]) == ls.capability_vocab()
    assert set(menu["taxonomy_ids"]) == ls.crosswalk_taxonomy_ids(_CROSSWALK)
