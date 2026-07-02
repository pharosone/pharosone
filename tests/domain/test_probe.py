import pytest
from pydantic import ValidationError

from probe_engine.domain.enums import ScenarioType, Severity, VariationStrategy
from probe_engine.domain.probe import (
    BinaryCheck,
    Evaluation,
    Probe,
    Provenance,
    Scenario,
    Turn,
)


def _probe(**over) -> Probe:
    base = dict(
        id="p1",
        title="t",
        severity="high",
        intent="induce forbidden tool call",
        taxonomy_tags=[{"system": "atlas", "id": "AML.T0051.001"}],
        scenario=Scenario(type="chain", turns=[Turn(role="user", seed_prompts=["hi"])]),
        evaluation=Evaluation(binary=BinaryCheck(oracle="tool_called", args={"tool": "x"})),
        provenance=Provenance(source="AgentDyn"),
    )
    base.update(over)
    return Probe(**base)


def test_probe_defaults():
    p = _probe()
    assert p.version == 1
    assert p.severity is Severity.HIGH
    assert p.scenario.type is ScenarioType.CHAIN
    assert p.applicability.industries == ["any"]
    assert p.variation.strategy == [VariationStrategy.DETERMINISTIC]
    assert p.variation.n_variants == 5


def test_probe_requires_at_least_one_evaluation():
    with pytest.raises(ValidationError):
        _probe(evaluation=Evaluation())


def test_probe_parses_taxonomy_tags():
    p = _probe()
    assert p.taxonomy_tags[0].id == "AML.T0051.001"


def test_probe_forbids_unknown_field():
    with pytest.raises(ValidationError):
        _probe(nonsense=True)
