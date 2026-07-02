from probe_engine.compile.compiler import compile_probe, variant_input_text
from probe_engine.domain.evidence import Variant
from probe_engine.domain.probe import (
    BinaryCheck,
    Evaluation,
    Probe,
    Provenance,
    Scenario,
    Turn,
)
from probe_engine.domain.run import RunConfig, TargetConfig, Thresholds


def _probe() -> Probe:
    return Probe(
        id="p1", title="t", severity="high", intent="x",
        taxonomy_tags=[{"system": "atlas", "id": "AML.T0051.001"}],
        scenario=Scenario(type="chain", turns=[Turn(role="user", seed_prompts=["read notes"])]),
        evaluation=Evaluation(binary=BinaryCheck(oracle="tool_called", args={"tool": "move_file"})),
        provenance=Provenance(source="MCPTox"),
    )


def _variants():
    return [
        Variant(probe_id="p1", variant_id="p1#0", fingerprint="0a",
                rendered_turns=[{"role": "user", "poison": "do X", "prompts": ["please read notes"]}]),
        Variant(probe_id="p1", variant_id="p1#1", fingerprint="0b",
                rendered_turns=[{"role": "user", "poison": None, "prompts": ["summarize"]}]),
    ]


def _rc() -> RunConfig:
    return RunConfig(target=TargetConfig(tier="mock"), epochs=3, thresholds=Thresholds(),
                     run_id="r", timestamp="2026-06-22T00:00:00Z")


def test_variant_input_text_joins_turns():
    txt = variant_input_text(_variants()[0])
    assert "please read notes" in txt
    assert "do X" in txt


def test_compile_builds_task_with_samples_and_epochs():
    task = compile_probe(_probe(), _variants(), _rc())
    assert len(task.dataset) == 2
    assert task.dataset[0].id == "p1#0"
    assert task.dataset[0].metadata["fingerprint"] == "0a"
    assert task.epochs == 3
    assert task.solver is not None
    assert task.scorer is not None
