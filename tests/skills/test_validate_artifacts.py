"""Tests for the pharosone artifact validator (passport / seams).

These exercise real validation LOGIC, not field presence: each negative case is crafted so
that exactly one invariant is broken and the specific error message is asserted. The base
fixtures are faithful copies of the vocabulary-clean example-agent artifacts, so the positive
cases prove the schema matches real onboarding output. The validator is imported as the
package module ``probe_engine.onboarding.validate``.
"""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from probe_engine.plan.library_spec import CANONICAL_CAPABILITIES

REPO_ROOT = Path(__file__).resolve().parents[2]

import probe_engine.onboarding.validate as validator


# --- base fixtures (vocabulary-clean example-agent, faithful) -------------------------------

VALID_PASSPORT: dict = {
    "agent": "example-agent",
    "topology": "in_process_python",
    "language": "python",
    "entrypoint": "app.llm:QualifierLLM.run_turn",
    "surfaces_tool_calls": False,
    "source_modifiable": True,
    "live_backend_ok": False,
    "framework": "custom",
    "integrations": [
        "llm:llm",
        "crm:rest",
        "messaging:rest",
        "sqlite:db",
    ],
    "tools": [
        {"name": "reply_to_client", "capabilities": ["send_message"], "dangerous": True},
        {"name": "record_answers", "capabilities": ["record_write"], "dangerous": True},
        {"name": "set_status", "capabilities": ["status_change"], "dangerous": True},
        {"name": "handoff_to_manager", "capabilities": ["escalate"], "dangerous": True},
        {"name": "route_to_microservice", "capabilities": ["escalate"], "dangerous": True},
    ],
    "channels": ["message", "history", "ingested_record", "memory"],
    "blind_spots": ["tool_result", "retrieved_doc", "file_content", "image_content"],
    "system_prompt_path": "prompts/system_prompt.md",
    "defenses": ["prompt_layer:security_rules", "code_layer:app/security.py"],
}

VALID_SEAMS: list = [
    {
        "seam": "run_turn decision fn (param-inject)",
        "file": "app/llm.py:333",
        "narrowness": 1,
        "technique": "param_inject",
        "channels": ["message", "history", "ingested_record", "memory"],
        "recommended": True,
        "evidence": "pure decision fn, zero IO; real writes happen later in _apply_turn",
        "neutralize": "none",
    },
    {
        "seam": "LLM tool-dispatch parsing",
        "file": "app/llm.py:201",
        "narrowness": 2,
        "technique": "monkeypatch",
        "channels": ["message", "tool_result:*", "tool_result:crm", "card_field:lead_snapshot"],
        "recommended": False,
    },
    {
        "seam": "call_openrouter brain",
        "file": "app/llm.py:88",
        "narrowness": 3,
        "technique": "monkeypatch",
        "channels": [],
        "recommended": False,
    },
]


def _passport() -> dict:
    return copy.deepcopy(VALID_PASSPORT)


def _seams() -> list:
    return copy.deepcopy(VALID_SEAMS)


def _has(errors: list[str], needle: str) -> bool:
    return any(needle in error for error in errors)


# --- positive: base fixtures and real on-disk artifacts -------------------------------------


def test_valid_passport_has_no_errors():
    assert validator.validate_passport(_passport()) == []


def test_valid_seams_has_no_errors():
    assert validator.validate_seams(_seams()) == []


def test_composite_topology_is_accepted():
    passport = _passport()
    passport["topology"] = "local_http+in_process_python"
    passport["tool_dispatch_waist"] = "target_agent.tools:execute_tool"
    assert validator.validate_passport(passport) == []


def test_model_config_block_is_accepted():
    passport = _passport()
    passport["model_config"] = {
        "provider": "openrouter",
        "model": "anthropic/claude-sonnet-4",
        "key_env": "OPENROUTER_API_KEY",
    }
    assert validator.validate_passport(passport) == []


def test_real_example_agent_passport_validates():
    instance = validator.load_artifact(REPO_ROOT / "harness/example-agent/PASSPORT.md")
    assert validator.validate_passport(instance) == []


def test_real_example_agent_seams_validate():
    instance = validator.load_artifact(REPO_ROOT / "harness/example-agent/SEAMS.md")
    assert validator.validate_seams(instance) == []


# --- negative: passport mechanical (schema-driven) ------------------------------------------


def test_unknown_topology_fails_pattern():
    passport = _passport()
    passport["topology"] = "serverless"
    errors = validator.validate_passport(passport)
    assert _has(errors, "topology")
    assert _has(errors, "does not match pattern")


def test_capability_outside_vocabulary_fails_enum():
    passport = _passport()
    passport["tools"][0]["capabilities"] = ["teleport"]
    errors = validator.validate_passport(passport)
    assert _has(errors, "tools[0].capabilities[0]")
    assert _has(errors, "'teleport'")
    assert _has(errors, "is not one of")


def test_engine_capability_tokens_valid_and_bogus_caught():
    # After converging the capability vocabulary on the ENGINE's CANONICAL_CAPABILITIES, tokens like
    # `transfer` and `read_memory` are VALID — they are exactly what the corpus targets in
    # required_tools and what selection resolves. The validator must ACCEPT every engine token and
    # reject ONLY a token outside CANONICAL_CAPABILITIES (a typo or a stale "polished" alias).
    # engine tokens on their own validate clean (proves they are in the vocabulary)
    ok = _passport()
    ok["tools"][0]["capabilities"] = ["transfer", "read_memory"]
    assert validator.validate_passport(ok) == []

    # a single bogus token sitting NEXT TO valid engine tokens is caught with a concrete field path,
    # and it is the ONLY enum failure (the engine tokens beside it are not flagged). We count enum
    # errors rather than substring-checking for 'transfer', because the "is not one of [...]" message
    # necessarily lists every valid token (including transfer) — so absence must be proven by count.
    bad = _passport()
    bad["tools"][0]["capabilities"] = ["transfer", "read_memory", "not_a_real_capability"]
    errors = validator.validate_passport(bad)
    assert _has(errors, "tools[0].capabilities[2]")
    assert _has(errors, "'not_a_real_capability'")
    assert _has(errors, "is not one of")
    enum_errors = [e for e in errors if "is not one of" in e]
    assert len(enum_errors) == 1, f"only the bogus token should fail the enum, got: {enum_errors}"

    # A stale pre-convergence "polished" alias is now correctly rejected (it is not an engine token).
    stale = _passport()
    stale["tools"][0]["capabilities"] = ["funds_transfer"]
    stale_errors = validator.validate_passport(stale)
    assert _has(stale_errors, "tools[0].capabilities[0]")
    assert _has(stale_errors, "'funds_transfer'")
    assert _has(stale_errors, "is not one of")


def test_full_engine_capability_vocabulary_validates():
    # A passport whose tools collectively declare EVERY engine capability token must validate clean:
    # proves the schema enum accepts the entire real CANONICAL_CAPABILITIES vocabulary, not just the
    # handful used by the base fixture.
    passport = _passport()
    passport["tools"] = [
        {"name": f"tool_{cap}", "capabilities": [cap], "dangerous": False}
        for cap in sorted(CANONICAL_CAPABILITIES)
    ]
    assert validator.validate_passport(passport) == []


def test_schema_capability_enum_equals_engine_canonical_capabilities():
    # GUARD (enum <-> engine alignment): the passport schema's capability enum MUST equal the
    # engine's CANONICAL_CAPABILITIES token-for-token. Selection is an exact string match with no
    # aliasing, so any drift between the schema (what onboarding artifacts validate against) and the
    # engine (what the corpus targets + what selection resolves) silently breaks selection. Pinning
    # them here catches a future enum<->engine drift automatically, mirroring the alignment tests in
    # tests/plan/test_library_spec.py.
    schema = validator.load_schema("passport")
    enum_list = schema["$defs"]["capability"]["enum"]
    enum = set(enum_list)
    assert len(enum_list) == len(enum), f"duplicate capability in schema enum: {sorted(enum_list)}"
    engine = set(CANONICAL_CAPABILITIES)
    assert enum == engine, (
        "passport.schema.json capability enum has drifted from CANONICAL_CAPABILITIES.\n"
        f"  in schema only: {sorted(enum - engine)}\n"
        f"  in engine only: {sorted(engine - enum)}"
    )


def test_extra_top_level_field_rejected():
    passport = _passport()
    passport["attacker_note"] = "surprise"
    errors = validator.validate_passport(passport)
    assert _has(errors, "attacker_note")
    assert _has(errors, "additional property not allowed")


def test_extra_field_in_tool_rejected():
    passport = _passport()
    passport["tools"][2]["escalation_ok"] = True
    errors = validator.validate_passport(passport)
    assert _has(errors, "tools[2].escalation_ok")
    assert _has(errors, "additional property not allowed")


def test_missing_required_field_reported():
    passport = _passport()
    del passport["entrypoint"]
    errors = validator.validate_passport(passport)
    assert _has(errors, "entrypoint")
    assert _has(errors, "required property is missing")


def test_wrong_scalar_type_reported():
    passport = _passport()
    passport["surfaces_tool_calls"] = "yes"
    errors = validator.validate_passport(passport)
    assert _has(errors, "surfaces_tool_calls")
    assert _has(errors, "expected type 'boolean'")


def test_passport_channel_must_be_canonical():
    # Passport channels are strict canonical (no parameterized / card_field forms; those are
    # a seam-level concept only).
    passport = _passport()
    passport["channels"] = ["message", "card_field:lead_snapshot"]
    errors = validator.validate_passport(passport)
    assert _has(errors, "channels[1]")
    assert _has(errors, "is not one of")


def test_bad_integration_grammar_reported():
    passport = _passport()
    passport["integrations"] = ["crm:rest", "mystery_service"]
    errors = validator.validate_passport(passport)
    assert _has(errors, "integrations[1]")
    assert _has(errors, "does not match pattern")


def test_tool_requires_at_least_one_capability():
    passport = _passport()
    passport["tools"][0]["capabilities"] = []
    errors = validator.validate_passport(passport)
    assert _has(errors, "tools[0].capabilities")
    assert _has(errors, "minItems")


# --- negative: passport semantic (cross-field) ----------------------------------------------


def test_channels_and_blind_spots_must_be_disjoint():
    passport = _passport()
    passport["blind_spots"] = ["message", "retrieved_doc"]
    errors = validator.validate_passport(passport)
    assert _has(errors, "semantic")
    assert _has(errors, "disjoint")
    assert _has(errors, "message")


# --- negative: seams mechanical (schema-driven) ---------------------------------------------


def test_unknown_technique_fails_enum():
    seams = _seams()
    seams[1]["technique"] = "rewrite_response"
    errors = validator.validate_seams(seams)
    assert _has(errors, "[1].technique")
    assert _has(errors, "is not one of")


def test_bad_channel_base_fails_grammar():
    seams = _seams()
    seams[0]["channels"] = ["message", "not_a_channel"]
    errors = validator.validate_seams(seams)
    assert _has(errors, "[0].channels[1]")
    assert _has(errors, "does not match pattern")


def test_bad_channel_empty_param_fails_grammar():
    seams = _seams()
    seams[1]["channels"] = ["tool_result:"]
    errors = validator.validate_seams(seams)
    assert _has(errors, "channels[0]")
    assert _has(errors, "does not match pattern")


def test_narrowness_wrong_type_is_mechanical_error():
    seams = _seams()
    seams[0]["narrowness"] = "1"
    errors = validator.validate_seams(seams)
    assert _has(errors, "[0].narrowness")
    assert _has(errors, "expected type 'integer'")


def test_seam_missing_required_field_reported():
    seams = _seams()
    del seams[2]["file"]
    errors = validator.validate_seams(seams)
    assert _has(errors, "[2].file")
    assert _has(errors, "required property is missing")


def test_empty_seams_array_fails_min_items():
    errors = validator.validate_seams([])
    assert _has(errors, "minItems")


# --- negative: seams semantic (cross-field) -------------------------------------------------


def test_two_recommended_seams_rejected():
    seams = _seams()
    seams[1]["recommended"] = True
    errors = validator.validate_seams(seams)
    assert _has(errors, "semantic")
    assert _has(errors, "exactly one seam must have recommended=true")
    assert _has(errors, "found 2")


def test_zero_recommended_seams_rejected():
    seams = _seams()
    seams[0]["recommended"] = False
    errors = validator.validate_seams(seams)
    assert _has(errors, "exactly one seam must have recommended=true")
    assert _has(errors, "found 0")


def test_narrowness_out_of_range_is_semantic_error():
    seams = _seams()
    seams[0]["narrowness"] = 9
    errors = validator.validate_seams(seams)
    assert _has(errors, "semantic")
    assert _has(errors, "narrowness 9 is outside")


def test_narrowness_zero_out_of_range():
    seams = _seams()
    seams[0]["narrowness"] = 0
    errors = validator.validate_seams(seams)
    assert _has(errors, "narrowness 0 is outside")


def test_recommended_seam_must_have_a_channel():
    seams = _seams()
    seams[0]["channels"] = []
    errors = validator.validate_seams(seams)
    assert _has(errors, "semantic")
    assert _has(errors, "recommended seam")
    assert _has(errors, "at least one channel")


def test_empty_channels_ok_on_non_recommended_seam():
    # The base fixture already has an empty-channel non-recommended seam; keep it valid.
    seams = _seams()
    assert seams[2]["channels"] == []
    assert validator.validate_seams(seams) == []


# --- artifact loading (markdown extraction + json) ------------------------------------------


def test_load_artifact_from_markdown(tmp_path: Path):
    md = tmp_path / "PASSPORT.md"
    md.write_text(
        "# Heading\n\nsome prose\n\n```json\n"
        + json.dumps(VALID_PASSPORT)
        + "\n```\n\nmore prose\n",
        encoding="utf-8",
    )
    instance = validator.load_artifact(md)
    assert instance == VALID_PASSPORT
    assert validator.validate_passport(instance) == []


def test_load_artifact_from_plain_json(tmp_path: Path):
    js = tmp_path / "seams.json"
    js.write_text(json.dumps(VALID_SEAMS), encoding="utf-8")
    instance = validator.load_artifact(js)
    assert instance == VALID_SEAMS
    assert validator.validate_seams(instance) == []


def test_load_artifact_no_json_block_raises(tmp_path: Path):
    md = tmp_path / "PASSPORT.md"
    md.write_text("# Heading\n\nno machine block here\n", encoding="utf-8")
    with pytest.raises(validator.ArtifactError):
        validator.load_artifact(md)


def test_load_artifact_invalid_json_raises(tmp_path: Path):
    js = tmp_path / "passport.json"
    js.write_text("{ not: valid json, }", encoding="utf-8")
    with pytest.raises(validator.ArtifactError):
        validator.load_artifact(js)


# --- CLI (exit codes end-to-end via subprocess) ---------------------------------------------


def _run_cli(kind: str, path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "probe_engine.onboarding.validate", kind, str(path)],
        capture_output=True,
        text=True,
    )


def test_cli_valid_artifact_exits_zero(tmp_path: Path):
    js = tmp_path / "passport.json"
    js.write_text(json.dumps(VALID_PASSPORT), encoding="utf-8")
    result = _run_cli("passport", js)
    assert result.returncode == 0
    assert "OK" in result.stdout


def test_cli_invalid_artifact_exits_one(tmp_path: Path):
    broken = copy.deepcopy(VALID_PASSPORT)
    broken["topology"] = "serverless"
    js = tmp_path / "passport.json"
    js.write_text(json.dumps(broken), encoding="utf-8")
    result = _run_cli("passport", js)
    assert result.returncode == 1
    assert "INVALID" in result.stderr
    assert "topology" in result.stderr


def test_cli_missing_file_exits_one(tmp_path: Path):
    result = _run_cli("seams", tmp_path / "does_not_exist.json")
    assert result.returncode == 1
    assert "not found" in result.stderr
