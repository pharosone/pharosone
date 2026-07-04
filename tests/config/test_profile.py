from pathlib import Path

from probe_engine.config.profile import RunProfile, load_profile, run_config_from_profile
from probe_engine.corpus.loader import load_corpus
from probe_engine.domain.run import RunConfig, TargetConfig, Thresholds, ToolSpec
from probe_engine.run.executor import run_probe
from probe_engine.run.selection import scope_excluded, select_probes
from probe_engine.sandbox.tools import sandbox_tools

ROOT = Path(__file__).parents[2]
FIN = ROOT / "configs" / "profiles" / "finance-support-agent.yaml"
CORPUS = ROOT / "corpus" / "probes"


def test_load_finance_profile():
    p = load_profile(FIN)
    assert p.industry == "financial_services"
    assert p.target.system_prompt and "ACME" in p.target.system_prompt
    assert {"create_payment_mandate", "schedule_transfer"} <= {t.name for t in p.tools}


def test_run_config_from_profile_derives_inventory_and_selection():
    rc = run_config_from_profile(load_profile(FIN), "r", "t")
    assert rc.industry == "financial_services"
    assert set(rc.available_tools) == {t.name for t in rc.tool_inventory}
    assert "create_payment_mandate" in rc.available_tools
    assert rc.target.system_prompt is not None


def test_sandbox_tools_from_declared_inventory():
    tools = sandbox_tools(None, [
        ToolSpec(name="wire_money", description="x", dangerous=True),
        ToolSpec(name="lookup", description="y"),
    ])
    assert {getattr(t, "name", None) for t in tools} == {"wire_money", "lookup"}


def test_sandbox_tools_default_when_no_inventory():
    assert len(sandbox_tools(None, None)) == 3  # default move_file/read_file/send_message


def test_profile_defaults_to_all_approaches():
    """A profile that omits `approaches` runs all three scenario families (backward compatible)."""
    rc = run_config_from_profile(load_profile(FIN), "r", "t")
    assert rc.approaches == ["single_turn", "chain", "adaptive"]


def test_profile_approaches_threads_and_narrows_selection():
    """A profile that narrows `approaches` threads into RunConfig and gates the corpus accordingly —
    the deselected families become scope exclusions, never silent passes."""
    prof = RunProfile(approaches=["single_turn"])
    rc = run_config_from_profile(prof, "r", "t")
    assert rc.approaches == ["single_turn"]
    corpus = load_corpus(CORPUS)
    selected = select_probes(corpus, rc)
    assert selected, "single-turn probes should still select"
    assert all(p.scenario.type.value == "single_turn" for p in selected)
    excluded = scope_excluded(corpus, rc)
    assert excluded, "chain/adaptive probes should be surfaced as scope exclusions"
    assert {p.scenario.type.value for p in excluded} <= {"chain", "adaptive"}


def test_finance_profile_selects_finance_excludes_healthcare():
    rc = run_config_from_profile(load_profile(FIN), "r", "t")
    ids = {p.id for p in select_probes(load_corpus(CORPUS), rc)}
    assert "mcptox-unauthorized-payment" in ids   # financial + tools available
    assert "atb-pii-exfil" not in ids             # healthcare -> excluded


def test_planner_synthesis_config_defaults():
    rc = run_config_from_profile(load_profile(FIN), "r", "t")
    # purely additive: existing profiles keep working with deterministic defaults
    assert rc.planner == "deterministic"
    assert rc.max_trials is None
    assert rc.synthesize_n == 0
    assert rc.target.planner_model is None
    assert rc.target.synthesis_model is None
    # default model = Opus 4.8 for both planner and synthesis
    assert rc.target.resolved_planner_model() == "anthropic/claude-opus-4-8"
    assert rc.target.resolved_synthesis_model() == "anthropic/claude-opus-4-8"


def test_profile_threads_planner_synthesis_fields(tmp_path):
    """A profile that DECLARES the planner/synthesis fields threads them into the RunConfig
    (planner-level) and the TargetConfig (model overrides), resolving the right models."""
    prof_yaml = (
        "name: p\n"
        "industry: financial_services\n"
        "target:\n"
        "  tier: model\n"
        "  model: anthropic/claude-3.5-sonnet\n"
        "  planner_model: anthropic/claude-3.5-haiku\n"
        "  synthesis_model: anthropic/claude-3.5-haiku\n"
        "planner: llm\n"
        "max_trials: 120\n"
        "synthesize_n: 3\n"
    )
    p = tmp_path / "prof.yaml"
    p.write_text(prof_yaml, encoding="utf-8")
    rc = run_config_from_profile(load_profile(p), "r", "t")
    assert rc.planner == "llm"
    assert rc.max_trials == 120
    assert rc.synthesize_n == 3
    assert rc.target.resolved_planner_model() == "anthropic/claude-3.5-haiku"
    assert rc.target.resolved_synthesis_model() == "anthropic/claude-3.5-haiku"


def test_profile_planner_synthesis_defaults_when_omitted(tmp_path):
    """A profile that omits the new fields keeps the inert deterministic defaults (back-compat)."""
    p = tmp_path / "prof.yaml"
    p.write_text("name: p\nindustry: any\n", encoding="utf-8")
    rc = run_config_from_profile(load_profile(p), "r", "t")
    assert rc.planner == "deterministic"
    assert rc.max_trials is None
    assert rc.synthesize_n == 0
    assert rc.target.resolved_planner_model() == "anthropic/claude-opus-4-8"
    assert rc.target.resolved_synthesis_model() == "anthropic/claude-opus-4-8"


def test_system_prompt_reaches_model_agent(tmp_path):
    probe = {p.id: p for p in load_corpus(CORPUS)}["agentdyn-important-instructions"]
    rc = RunConfig(
        target=TargetConfig(tier="model", model="mockllm/model",
                            system_prompt="PERSONA_MARKER_42"),
        n_variants=1, epochs=1, thresholds=Thresholds(), run_id="r", timestamp="t",
    )
    ev = run_probe(probe, rc, seed=1, log_dir=str(tmp_path / "logs"))
    assert any("PERSONA_MARKER_42" in m["content"] for m in ev.transcript if m["role"] == "system")
