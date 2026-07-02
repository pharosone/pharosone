# Variation-strategy benchmark

Decide — **with data, not priors** — how the engine should pick `(frame × payload × obfuscation)`
when expanding a probe into variants:

| strategy | policy |
|---|---|
| `curated` | the **current production** policy (`techniques.diversify`): a hand-written table of 18 `Technique`s that couple a frame to a fixed obfuscator set; even index = plain, odd = obfuscated. |
| `naive` | Ilya's literal proposal: random frame + random payload + a random chain of 1–3 obfuscators drawn **uniformly from all 13**, independent of the frame. Max breadth, zero curation. |
| `compat` | the proposed middle: combinatorial freedom **within coherence** — keep the plain/obf split, but draw obf chains only from obfuscators compatible with the frame's style, capped by the payload's oracle sensitivity. |

All three draw from the **same real primitives** (`variation/techniques.py` frames, `variation/obfuscate.py`
obfuscators). They differ *only* in the selection policy, so the experiment isolates exactly the thing
in dispute. `curated` is pinned byte-for-byte against the real `diversify` by a test.

## Run

```bash
uv run python -m benchmarks.variation_strategies.run                    # defaults: budgets 8,32,128
uv run python -m benchmarks.variation_strategies.run --budgets 8,32,128 --seeds 5 --payloads 20 \
    --out reports/variation_bench.json
```

Offline, deterministic, no API keys, no network.

## What it measures

For each `strategy × defense × budget × seed`:

- **recall** = distinct findings / discoverable holes — the headline. *Of everything findable, how
  much did this policy find at this budget?*
- **efficiency** = distinct findings / trials — bugs per trial (the budget-economy question).
- **dup_rate** = `1 − distinct/raw_hits` — budget wasted re-finding the **same** hole. This is the
  number that operationalizes "40 obfuscation variants find the same bug 40 times."
- **reproducibility** — same seed ⇒ identical output multiset (required for an audit artifact).

A **finding** is a distinct `(payload, hole)` pair. The target is a panel of **synthetic defense
archetypes** (`defenses.py`), each scored on intrinsic attack *features* — never on which strategy
produced the attack, so it cannot be rigged. Each encodes one hypothesis from the design debate:

| defense | hypothesis it tests |
|---|---|
| `keyword_guard` | filters plaintext keywords; obfuscation bypasses — but many obfuscators collapse to **one** hole (rewards obfuscation, exposes dup waste). |
| `frame_blocklist` | knows public frames; only **novel framing** bypasses; obfuscation is irrelevant (rewards frame diversity). |
| `semantic_classifier` | detects intent regardless of surface; only a few high-level techniques evade and **over-garbling raises suspicion** (punishes naive). |
| `brittle_parser` | falls to moderate attacks, but **heavy obfuscation makes the agent no-op** (obfuscation defeats the payload, not just the filter). |
| `naive_agent` | no defense at all; fails only on nonsense input (sanity check — shows naive's self-harm). |

The panel is deliberately balanced: some defenses reward obfuscation breadth, others punish it, others
only reward framing novelty. So **no single strategy can win everything** — the output shows *under
which defense regime each policy wins*, which is the honest, decision-useful result.

## What it does NOT prove

This is a **simulation**. The defenses are hypotheses about how real agents fail, not measured
ground truth. Its job is to (1) **rule out** a dominated strategy cheaply, (2) show *where* each policy
wins and loses, and (3) be **calibrated/replaced by a small live-model run** before any architecture
change ships. The LLM-vs-static *cost* axis is a separate benchmark (this one holds variation
deterministic); `obf_ops` is only a compute proxy. Treat the verdict as "which hypotheses to test
live," not "the final answer."
