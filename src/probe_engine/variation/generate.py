"""Expand a probe into N distinct deduplicated variants (spec §8).

CONTEXT-BOUND diverse variation is the DEFAULT for EVERY payload (decisions 1/8/9): both each
turn's ``seed_prompts`` AND its ``poison`` are diversified, so every variant is a genuinely
different attack seeded by the corpus payload — poison is NO LONGER copied static. Each variant's
technique is selected by ``variant_index`` parity (even=plain reformulation, odd=obfuscated;
decision 8) and is bound to the target ``AgentContext`` (domain / tool names / guardrails;
decision 9). Variation is always SEEDED and offline by default via
``techniques.diversify``.

Pass ``mutate`` (a ``(text, lang, index) -> str`` callable, e.g. from
``make_llm_attack_mutator``) to drive variation with an LLM instead; it must already fall back to
the deterministic context-bound diversifier on model error/refusal/empty (decision 5), so this
module stays robust. Either way the same ``(text, lang, variant_index)`` call shape is used.

The semantic invariant lives in the oracle / judge (decision 2), so free, diversified,
possibly-obfuscated rewrites are safe — success is judge-decided."""

import random
from typing import Callable

from pydantic import BaseModel

from probe_engine.domain.enums import VariationStrategy
from probe_engine.domain.evidence import Variant
from probe_engine.domain.probe import Probe
from probe_engine.targets.agent_context import AgentContext
from probe_engine.variation.mutators import fingerprint
from probe_engine.variation.techniques import diversify

# A diversified attack mutator: (text, lang, variant_index) -> rewritten attack. Same shape as
# make_llm_attack_mutator's returned callable; the deterministic default is techniques.diversify.
AttackMutateFn = Callable[[str, str, int], str]


class VariationMeta(BaseModel):
    requested: int
    produced: int
    exhausted: bool


def generate_variants(
    probe: Probe,
    n: int,
    seed: int = 0,
    languages: list[str] | None = None,
    max_attempts_per_variant: int = 20,
    context: AgentContext | None = None,
    mutate: AttackMutateFn | None = None,
    fanout_channels: list[str] | None = None,
) -> tuple[list[Variant], VariationMeta]:
    """Build up to ``n`` distinct variants of ``probe``.

    Every payload (poison AND seed_prompts) of every turn is diversified with the context-bound,
    seeded diversifier (or ``mutate`` when given). The plain-vs-obfuscated FAMILY is keyed on the
    variant's index PARITY, so it is held constant across that variant's prompts AND poison (a
    variant is wholly plain OR wholly obfuscated) and both categories appear across the probe's
    variants (decision 8). The specific technique within that family may rotate per payload — that
    is additional diversity, not incoherence; the load-bearing invariant is the family split.

    ``fanout_channels`` (the target's declared channels, ∪ message) drives MULTI-CHANNEL turns
    (``turn.multi_channel``): the turn's poison is rendered ONCE PER CHANNEL with a DISTINCT index
    (``idx*K + c``), so each surface carries a genuinely different rewrite (Option B). Because those
    per-channel indices have mixed parity, the family invariant is deliberately RELAXED *within* a
    multi-channel turn — looking different across surfaces is the whole point (evade per-channel
    dedup/detection). The result is stashed as ``channel_payloads`` on the rendered turn; ``poison``
    keeps the first channel's variation so the input text / mock tier / single-channel fallback all
    still work."""
    langs = languages or probe.variation.languages or ["en"]
    use_llm = mutate is not None
    variants: list[Variant] = []
    seen: set[str] = set()
    salt = 0
    budget = max(n * max_attempts_per_variant, max_attempts_per_variant)

    def _render(p: str, rng: random.Random, lang: str, idx: int) -> str:
        # Diversify a single payload, context-bound, with the plain/obfuscated MIX keyed on idx.
        if use_llm:
            out = mutate(p, lang, idx)  # LLM attack-mutator (already falls back internally)
            if out:
                return out
        return diversify(p, rng, lang=lang, context=context, variant_index=idx)

    def _fanout(poison: str, rng: random.Random, lang: str, idx: int) -> dict[str, str]:
        # One distinct variation per channel (Option B). idx*K+c keeps every (variant, channel)
        # index unique within a run (K is fixed), and its alternating parity mixes families across
        # surfaces on purpose. Dedup so a target that declares a channel twice doesn't double-deliver.
        chans = list(dict.fromkeys(fanout_channels or ["message"]))
        k = len(chans)
        return {ch: _render(poison, rng, lang, idx * k + c) for c, ch in enumerate(chans)}

    while len(variants) < n and salt < budget:
        lang = langs[len(variants) % len(langs)]
        rng = random.Random(str((seed, salt)))  # tuple seeds rejected in Python 3.12+; str() adapts
        salt += 1
        # variant_index drives the technique MIX: the plain-vs-obfuscated FAMILY is held constant
        # across this variant's prompts AND poison (decision 8 — wholly plain OR wholly obfuscated,
        # keyed on idx parity). The specific technique within the family may rotate per payload.
        idx = len(variants)

        rendered_turns: list[dict] = []
        parts: list[str] = []
        for turn in probe.scenario.turns:
            mutated_prompts = [_render(p, rng, lang, idx) for p in turn.seed_prompts]
            channel_payloads: dict[str, str] | None = None
            if turn.poison and turn.multi_channel and fanout_channels and len(
                set(fanout_channels)
            ) > 1:
                channel_payloads = _fanout(turn.poison, rng, lang, idx)
                mutated_poison = next(iter(channel_payloads.values()))  # representative
            else:
                mutated_poison = _render(turn.poison, rng, lang, idx) if turn.poison else None
            rendered_turn = {
                "role": turn.role,
                "poison": mutated_poison,
                "prompts": mutated_prompts,
                "channel": turn.channel,
            }
            if channel_payloads is not None:
                rendered_turn["channel_payloads"] = channel_payloads
            rendered_turns.append(rendered_turn)
            parts.extend(mutated_prompts)
            if channel_payloads is not None:
                parts.extend(channel_payloads.values())  # all surfaces -> distinct fingerprint
            elif mutated_poison:
                parts.append(mutated_poison)

        fp = fingerprint(f"{lang}||" + "||".join(parts))
        if fp in seen:
            continue
        seen.add(fp)
        variants.append(
            Variant(
                probe_id=probe.id,
                variant_id=f"{probe.id}#{len(variants)}",
                language=lang,
                strategy=VariationStrategy.LLM if use_llm else VariationStrategy.DETERMINISTIC,
                mutation_seed=seed,
                fingerprint=fp,
                rendered_turns=rendered_turns,
            )
        )

    meta = VariationMeta(requested=n, produced=len(variants), exhausted=len(variants) < n)
    return variants, meta
