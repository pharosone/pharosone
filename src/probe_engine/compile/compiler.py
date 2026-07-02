"""Compile a probe + its variants into an executable Inspect Task (spec §3.2)."""

from typing import Any, Awaitable, Callable

from inspect_ai import Epochs, Task
from inspect_ai.dataset import MemoryDataset, Sample

from probe_engine.domain.evidence import Variant
from probe_engine.domain.probe import Probe
from probe_engine.domain.run import RunConfig
from probe_engine.scoring.oracle import probe_oracle_scorer
from probe_engine.targets.mock import MockPolicy
from probe_engine.targets.registry import build_target_solver


def variant_input_text(variant: Variant) -> str:
    parts: list[str] = []
    for turn in variant.rendered_turns:
        for prompt in turn.get("prompts", []):
            parts.append(prompt)
        if turn.get("poison"):
            parts.append(turn["poison"])
    return "\n".join(parts)


def compile_probe(
    probe: Probe,
    variants: list[Variant],
    run_config: RunConfig,
    mock_policy: MockPolicy | None = None,
    api_key: str | None = None,
    external: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
) -> Task:
    samples = [
        Sample(
            input=variant_input_text(v),
            target="robust",
            id=v.variant_id,
            # carry the structured turns so targets can execute them turn-by-turn
            # (plant -> trigger), instead of treating the flattened text as one prompt.
            metadata={
                "probe_id": probe.id,
                "fingerprint": v.fingerprint,
                "scenario": probe.scenario.type.value,
                "turns": v.rendered_turns,
                # authz/stateful facts (deployment-side) so every target tier seeds the same
                # offline ownership backend identically (see sandbox.state.seed_from_meta).
                "acting_principal": (
                    run_config.target.acting_principal.model_dump()
                    if run_config.target.acting_principal else None
                ),
                "resource_owners": run_config.target.resource_owners,
                "authz_default": run_config.target.authz_default,
                "seed_stage": run_config.target.seed_stage,
                "lifecycle_order": run_config.target.lifecycle_order,
                "lifecycle_floor": run_config.target.lifecycle_floor,
            },
        )
        for v in variants
    ]
    # Reference text the prompt_leak oracle guards: the agent's declared secret snippets plus its
    # system prompt (used only by prompt_leak; ignored by other oracles). Same reference the mock
    # target reads to simulate a leak (built once on TargetConfig, used in both places).
    protected = run_config.target.protected_reference()
    return Task(
        dataset=MemoryDataset(samples),
        solver=build_target_solver(probe, run_config, mock_policy, api_key, external),
        scorer=probe_oracle_scorer(
            probe.evaluation.binary,
            protected,
            semantic=probe.evaluation.semantic,
            judge_model=run_config.target.resolved_judge_model(),
            api_key=api_key,
        ),
        epochs=Epochs(run_config.epochs, ["mean"]),
    )
