"""B3 follow-up — the resume config-hash must capture DEPLOYMENT IDENTITY, not just depth.

Regression for the review finding: endpoint / protocol / provider were omitted from the
result-affecting config, so a resumed bridge/model run that re-pointed at a different agent or a
different provider-prefixed model silently reused stale Evidence. All offline, no network.
"""

from probe_engine.domain.probe import (
    BinaryCheck,
    Evaluation,
    Probe,
    Provenance,
    Scenario,
    Turn,
)
from probe_engine.domain.run import RunConfig, TargetConfig, Thresholds
from probe_engine.run import checkpoint


def _probe(pid: str = "p1") -> Probe:
    return Probe(
        id=pid, title="t", severity="high", intent="x",
        taxonomy_tags=[{"system": "atlas", "id": "AML.T0051.001"}],
        scenario=Scenario(type="chain", turns=[Turn(role="user", seed_prompts=["hi"])]),
        evaluation=Evaluation(binary=BinaryCheck(oracle="tool_called", args={"tool": "move_file"})),
        provenance=Provenance(source="MCPTox"),
    )


def _rc(**target_kwargs) -> RunConfig:
    return RunConfig(
        target=TargetConfig(tier="bridge", **target_kwargs),
        n_variants=2, epochs=1,
        thresholds=Thresholds(), run_id="r", timestamp="2026-06-25T00:00:00Z",
    )


def _hash(rc: RunConfig) -> str:
    return checkpoint.config_hash(_probe(), rc, 2, 1, 1)


def test_same_deployment_same_hash():
    rc = _rc(endpoint="http://a", protocol="openai", provider=None)
    assert _hash(rc) == _hash(_rc(endpoint="http://a", protocol="openai", provider=None))


def test_endpoint_change_busts_hash():
    # bridge tier: a different endpoint is a DIFFERENT agent -> must NOT reuse stale Evidence.
    assert _hash(_rc(endpoint="http://a")) != _hash(_rc(endpoint="http://b"))


def test_protocol_change_busts_hash():
    assert _hash(_rc(protocol="openai")) != _hash(_rc(protocol="anthropic"))


def test_provider_change_busts_hash():
    # provider prefixes the resolved model/judge slug -> changes the effective model the run uses.
    assert _hash(_rc(provider=None)) != _hash(_rc(provider="openrouter"))
