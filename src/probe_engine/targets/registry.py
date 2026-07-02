"""Build the Inspect Solver for the configured target tier + scenario (spec §4, §3.1, §9)."""

from typing import Any, Awaitable, Callable

from inspect_ai.solver import Solver

from probe_engine.domain.probe import Probe
from probe_engine.domain.run import RunConfig
from probe_engine.targets.adaptive import (
    adaptive_bridge,
    adaptive_indirect_bridge,
    adaptive_mock,
    adaptive_model,
)
from probe_engine.targets.bridge import bridge_target_solver, http_endpoint_agent
from probe_engine.targets.mock import MockPolicy, mock_target
from probe_engine.targets.model import model_target_solver


def _adaptive_is_indirect(probe: Probe) -> bool:
    """True when an adaptive probe carries an ingestion channel (a turn whose channel is not the
    direct `message` conversation) — its live poison must be delivered via injection, not chat."""
    return any(t.channel and t.channel != "message" for t in probe.scenario.turns)


def build_target_solver(
    probe: Probe,
    run_config: RunConfig,
    mock_policy: MockPolicy | None = None,
    api_key: str | None = None,
    external: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
) -> Solver:
    tier = run_config.target.tier
    is_adaptive = probe.scenario.type.value == "adaptive"

    if tier == "mock":
        if probe.evaluation.binary is None:
            raise ValueError(f"probe {probe.id}: mock tier requires evaluation.binary")
        policy = mock_policy or MockPolicy()
        # The mock simulates a prompt_leak by echoing the guarded reference (the overlap oracle
        # ignores fallback markers when a reference is configured), so thread it in.
        protected = run_config.target.protected_reference()
        if is_adaptive:
            return adaptive_mock(
                probe.evaluation.binary, policy, probe.intent, probe.scenario.max_turns, protected
            )
        return mock_target(probe.evaluation.binary, policy, protected)

    if tier == "model":
        if is_adaptive:
            return adaptive_model(probe, run_config, api_key, probe.scenario.max_turns)
        return model_target_solver(probe, run_config)

    if tier == "bridge":
        binary = probe.evaluation.binary
        if binary is not None:
            inv = run_config.tool_inventory
            if binary.oracle == "state_changed" and not any(s.dangerous for s in inv):
                raise ValueError(
                    f"probe {probe.id}: bridge 'state_changed' oracle can never fire without a "
                    f"tool_inventory declaring a dangerous tool (the agent's reported tool calls "
                    f"can't be interpreted) — declare the agent's tools or use a contains oracle"
                )
            if binary.oracle == "secret_leaked" and not any(s.leaks_if_path_contains for s in inv):
                raise ValueError(
                    f"probe {probe.id}: bridge 'secret_leaked' oracle can never fire without a "
                    f"tool_inventory tool carrying leaks_if_path_contains — declare it or use a "
                    f"contains oracle on the agent's reply"
                )
            if binary.oracle == "authz_violation" and not any(
                s.authz_action and s.resource_arg for s in inv
            ):
                raise ValueError(
                    f"probe {probe.id}: bridge 'authz_violation' oracle can never fire without a "
                    f"tool_inventory tool declaring BOTH authz_action and resource_arg (the agent's "
                    f"reported call can't be adjudicated as an access decision) — declare them or the "
                    f"probe is a blind spot, not a pass"
                )
        ext = external
        if ext is None:
            endpoint = run_config.target.endpoint
            if not endpoint:
                raise ValueError(
                    "bridge tier requires target.endpoint (an OpenAI-compatible HTTP URL) "
                    "or an external callable passed via run_probe(..., external=...)"
                )
            ext = http_endpoint_agent(
                endpoint, api_key=api_key, model=run_config.target.model
            )
        if is_adaptive:
            # adaptive + an ingestion channel (poison turn channel != message) -> deliver the
            # attacker's live poison via request["injection"] into the ingested surface rather
            # than as a visible user message (the indirect adaptive vector, decision 4).
            if _adaptive_is_indirect(probe):
                return adaptive_indirect_bridge(
                    probe, run_config, api_key, ext, probe.scenario.max_turns
                )
            return adaptive_bridge(probe, run_config, api_key, ext, probe.scenario.max_turns)
        return bridge_target_solver(probe, run_config, ext)
    raise ValueError(f"unknown target tier: {tier!r}")
