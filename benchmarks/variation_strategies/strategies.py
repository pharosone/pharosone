"""The three variation SELECTION POLICIES under test.

All three draw from the SAME real primitives — the frames in ``probe_engine.variation.techniques``
and the obfuscators in ``probe_engine.variation.obfuscate`` — so the comparison isolates the one
thing in dispute: HOW you pick (frame, obfuscation-chain) for a payload. Nothing here invents a new
attack; it only changes the selection.

  * ``curated``    — the production policy: ``techniques.diversify`` exactly (frame+obf coupled per
                     hand-written Technique; even index = plain, odd = obfuscated). Re-expressed here
                     so it ALSO returns structure (frame id / family / obf chain) for scoring. A test
                     pins that its text is byte-identical to ``diversify`` for the same seed.
  * ``naive``      — Ilya's literal description: pick a random frame, a random payload, and a random
                     chain of 0..3 obfuscators drawn UNIFORMLY from all of them, independent of the
                     frame (encoding can stack). No coherence, no intensity cap. Drawing 0 yields a
                     PLAIN (zero-obfuscator) variant, so the cartesian is FAIR — naive is not punished
                     for always garbling (the old always-1..3 bias was a strawman vs the plain/obf
                     policies). It still has no plain/obf *split* (parity); plain is just one cell.
  * ``compat``     — the proposed middle: combinatorial freedom WITHIN coherence. Plain/obf split kept
                     (index parity); obf chains drawn only from obfuscators COMPATIBLE with the frame
                     style (matrix derived from the real curated pairings) and capped by the payload's
                     oracle sensitivity (a tool_arg-exact payload tolerates far less garble than a
                     "leak your prompt" one).

Determinism (house rule): every random choice flows through the passed ``random.Random``; no global
``random``, no ``time``/``Date.now``. Same seed -> identical output (a test pins this).
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass

from probe_engine.targets.agent_context import AgentContext
from probe_engine.variation import obfuscate as obf
from probe_engine.variation.techniques import (
    OBFUS_TECHNIQUES,
    PLAIN_TECHNIQUES,
    Technique,
    _splice_context,
    diversify,
    technique_for,
)

ALL_TECHNIQUES: list[Technique] = PLAIN_TECHNIQUES + OBFUS_TECHNIQUES


@dataclass(frozen=True)
class Rendered:
    """A produced attack + its structural provenance (what the defenses score on)."""

    text: str
    frame_id: str          # the Technique.name used for framing
    frame_style: str       # coarse style group (see _FRAME_STYLE) — a defense feature
    family: str            # "plain" | "obf"
    obf_chain: tuple[str, ...]  # obfuscator names applied, in order


# ----------------------------------------------------------------------------------------------
# Frame style grouping + compatibility matrix.
# DERIVED FROM the real curated pairings in OBFUS_TECHNIQUES (each technique already encodes which
# obfuscators cohere with its framing) and generalized to a style -> {obfuscators} matrix. This is
# the curation knowledge made explicit, not invented.
# ----------------------------------------------------------------------------------------------
_FRAME_STYLE: dict[str, str] = {
    # plain family
    "domain_reframe": "plain_generic",
    "authority_frame": "authority",
    "urgency_frame": "plain_generic",
    "compliance_frame": "authority",
    "casual_frame": "plain_generic",
    "conflict_frame": "hierarchy",
    "roleplay_frame": "roleplay",
    "translate_launder": "multilingual",
    # obfuscated family
    "zero_width_t": "system_note",
    "leet_t": "roleplay",
    "encoding_t": "decode",
    "multilingual_t": "multilingual",
    "fullwidth_t": "authority",
    "bidi_t": "system_note",
    "combining_t": "system_note",
    "hex_t": "decode",
    "flooding_t": "transcript",
}

_STYLE_OBFUSCATORS: dict[str, tuple[str, ...]] = {
    "decode": ("base64_wrap", "hex_wrap"),
    "system_note": ("zero_width", "spacing", "bidi_wrap", "combining_marks", "markup_smuggle"),
    "authority": ("fullwidth", "char_split"),
    "roleplay": ("leet", "char_split"),
    "multilingual": ("markup_smuggle", "fullwidth"),
    "transcript": ("context_flood", "delimiter_break"),
    "hierarchy": ("markup_smuggle",),
    "plain_generic": (),
}

# Styles that can carry obfuscation (used when compat needs an obfuscated frame).
_OBF_CAPABLE_STYLES = [s for s, o in _STYLE_OBFUSCATORS.items() if o]

# Oracle sensitivity -> max obfuscation-chain length the payload tolerates before the agent can no
# longer act on the instruction (modelled in defenses.py as parse-break). Tighter for exact-match
# oracles, looser for purely behavioral ones.
SENSITIVITY_CAP: dict[str, int] = {"high": 1, "med": 2, "low": 3}


def _style_of(technique: Technique) -> str:
    return _FRAME_STYLE.get(technique.name, "plain_generic")


StrategyFn = Callable[[str, random.Random, AgentContext, int, str], Rendered]


# ----------------------------------------------------------------------------------------------
# 1. CURATED — production policy (techniques.diversify), exposed structurally.
# ----------------------------------------------------------------------------------------------
def curated(payload: str, rng: random.Random, ctx: AgentContext, idx: int, sensitivity: str) -> Rendered:
    """The current default. Body mirrors ``diversify``'s context branch so text is faithful; a test
    asserts byte-equality with ``diversify`` for the same (payload, seed, ctx, idx)."""
    technique = technique_for(idx, rng)
    text = _splice_context(payload, ctx, technique, rng, "en")
    if technique.is_obfuscated:
        for name in technique.obfuscators:
            text = obf.OBFUSCATORS[name](text, rng)
    return Rendered(
        text=text,
        frame_id=technique.name,
        frame_style=_style_of(technique),
        family="obf" if technique.is_obfuscated else "plain",
        obf_chain=technique.obfuscators,
    )


# ----------------------------------------------------------------------------------------------
# 2. NAIVE CARTESIAN — Ilya's literal "pick one from each pool and mix" with no coherence.
# ----------------------------------------------------------------------------------------------
def naive(payload: str, rng: random.Random, ctx: AgentContext, idx: int, sensitivity: str) -> Rendered:
    """Random frame (any family) + random 0..3 obfuscators drawn UNIFORMLY from all 13, independent
    of the frame and ignoring payload sensitivity. Encoding can stack on encoding. This is the
    pure-combinatorial baseline — maximal breadth, zero curation.

    FAIRNESS FIX: the chain length is drawn from 0..3 (was 1..3), so a PLAIN zero-obfuscator variant
    is a reachable cell of the cartesian. The old always-obfuscate bias punished naive on agents that
    fall to a clean reframing but survive garble, making it a strawman against the plain/obf-split
    policies; allowing k=0 removes that bias. Still fully seeded/deterministic (same seed -> same
    output) — only the draw range changed."""
    technique = rng.choice(ALL_TECHNIQUES)
    text = _splice_context(payload, ctx, technique, rng, "en")
    k = rng.randint(0, 3)
    chain = tuple(rng.sample(obf.OBFUSCATOR_NAMES, k=k)) if k else ()
    for name in chain:
        text = obf.OBFUSCATORS[name](text, rng)
    return Rendered(
        text=text,
        frame_id=technique.name,
        frame_style=_style_of(technique),
        family="obf" if chain else "plain",  # k==0 -> a fair PLAIN variant
        obf_chain=chain,
    )


# ----------------------------------------------------------------------------------------------
# 3. COMPAT COMBINATORIAL — combinatorial within coherence + sensitivity cap.
# ----------------------------------------------------------------------------------------------
def compat(payload: str, rng: random.Random, ctx: AgentContext, idx: int, sensitivity: str) -> Rendered:
    """Keep the plain/obf split (index parity). For obfuscated variants pick ANY obf-capable frame
    style, then draw the chain ONLY from that style's compatible obfuscators, capped by the payload's
    sensitivity. Wider than curated (frame and chain are recombined, not a fixed 18-pair table) but
    coherent (base64 only under a decode frame, etc.) and never over-garbling an exact-match payload."""
    if idx % 2 == 0:
        # plain reformulation: a non-obfuscated frame, no garble (mirrors curated's even branch).
        technique = rng.choice(PLAIN_TECHNIQUES)
        text = _splice_context(payload, ctx, technique, rng, "en")
        return Rendered(text, technique.name, _style_of(technique), "plain", ())

    style = rng.choice(_OBF_CAPABLE_STYLES)
    frames_in_style = [t for t in ALL_TECHNIQUES if _style_of(t) == style] or OBFUS_TECHNIQUES
    technique = rng.choice(frames_in_style)
    text = _splice_context(payload, ctx, technique, rng, "en")

    allowed = list(_STYLE_OBFUSCATORS[style])
    cap = min(SENSITIVITY_CAP.get(sensitivity, 2), len(allowed))
    k = rng.randint(1, max(1, cap))
    chain = tuple(rng.sample(allowed, k=min(k, len(allowed))))
    for name in chain:
        text = obf.OBFUSCATORS[name](text, rng)
    return Rendered(text, technique.name, style, "obf", chain)


# ----------------------------------------------------------------------------------------------
# 4. TUPLE-CURATED — the representation refactor: the curated table as explicit (frame, obf_chain)
#    tuples instead of bundled Technique objects. Behaviour is IDENTICAL to ``curated`` (same
#    plain/obf split, same selection, same rng order -> byte-identical output; a test pins this).
#    The WIN is purely structural and free: the policy is now a list of data tuples you can grow
#    (the monthly refresher just appends tuples — no code), every attack carries first-class
#    (frame_id, obf_chain) provenance for per-axis attribution, and a cert can record the compact
#    tuple instead of an opaque string. This is "orthogonal tuples as the representation, curated
#    subset as the policy" — Ilya's structural insight captured at zero behavioural cost.
# ----------------------------------------------------------------------------------------------
TECHNIQUE_BY_NAME: dict[str, Technique] = {t.name: t for t in ALL_TECHNIQUES}

# The curated 18 pairings, now as an explicit data table (derived IN ORDER from the real lists so
# selection indexes the same entry the production selector would). Split by family, exactly as
# techniques.technique_for splits PLAIN vs OBFUS by index parity.
CURATED_PLAIN: list[tuple[str, tuple[str, ...]]] = [(t.name, t.obfuscators) for t in PLAIN_TECHNIQUES]
CURATED_OBF: list[tuple[str, tuple[str, ...]]] = [(t.name, t.obfuscators) for t in OBFUS_TECHNIQUES]


def tuple_curated(payload: str, rng: random.Random, ctx: AgentContext, idx: int, sensitivity: str) -> Rendered:
    """Select from the explicit (frame_id, obf_chain) table. Replicates ``technique_for``'s rng order
    exactly (one ``randrange`` over the family, even=plain / odd=obf), so output equals ``curated``
    byte-for-byte — proving the data-model refactor preserves behaviour."""
    family = CURATED_PLAIN if idx % 2 == 0 else CURATED_OBF
    pos = (idx // 2 + rng.randrange(len(family))) % len(family)
    frame_id, obf_chain = family[pos]
    technique = TECHNIQUE_BY_NAME[frame_id]
    text = _splice_context(payload, ctx, technique, rng, "en")
    for name in obf_chain:
        text = obf.OBFUSCATORS[name](text, rng)
    return Rendered(
        text=text,
        frame_id=frame_id,
        frame_style=_style_of(technique),
        family="obf" if obf_chain else "plain",
        obf_chain=obf_chain,
    )


STRATEGIES: dict[str, StrategyFn] = {
    "curated": curated,
    "tuple-curated": tuple_curated,
    "naive": naive,
    "compat": compat,
}


def diversify_text(payload: str, rng: random.Random, ctx: AgentContext, idx: int) -> str:
    """The real ``diversify`` output — used by the faithfulness test to pin ``curated`` against it."""
    return diversify(payload, rng, lang="en", context=ctx, variant_index=idx)
