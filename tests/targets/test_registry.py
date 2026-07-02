import pytest

from probe_engine.domain.probe import (
    BinaryCheck,
    Evaluation,
    Probe,
    Provenance,
    Scenario,
    Turn,
)
from probe_engine.domain.run import RunConfig, TargetConfig, Thresholds
from probe_engine.targets.registry import build_target_solver


def _probe() -> Probe:
    return Probe(
        id="p1", title="t", severity="high", intent="x",
        taxonomy_tags=[{"system": "atlas", "id": "AML.T0051.001"}],
        scenario=Scenario(type="chain", turns=[Turn(role="user", seed_prompts=["hi"])]),
        evaluation=Evaluation(binary=BinaryCheck(oracle="tool_called", args={"tool": "x"})),
        provenance=Provenance(source="X"),
    )


def _rc(tier: str, **target_kwargs) -> RunConfig:
    return RunConfig(target=TargetConfig(tier=tier, **target_kwargs), thresholds=Thresholds(),
                     run_id="r", timestamp="2026-06-22T00:00:00Z")


def test_mock_tier_builds_a_solver():
    solver = build_target_solver(_probe(), _rc("mock"))
    assert solver is not None


def test_model_tier_builds_a_solver():
    solver = build_target_solver(_probe(), _rc("model"))
    assert solver is not None


def test_bridge_tier_requires_endpoint_or_callable():
    with pytest.raises(ValueError):
        build_target_solver(_probe(), _rc("bridge"))


def test_bridge_tier_builds_from_endpoint():
    solver = build_target_solver(
        _probe(), _rc("bridge", endpoint="https://agent.example/v1/chat/completions")
    )
    assert solver is not None


def test_bridge_tier_builds_from_external_callable():
    async def ext(_request):
        return {"choices": [{"message": {"content": "ok"}}]}

    solver = build_target_solver(_probe(), _rc("bridge"), external=ext)
    assert solver is not None


def test_unknown_tier_raises():
    with pytest.raises(ValueError):
        build_target_solver(_probe(), _rc("nope"))
