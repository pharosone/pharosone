"""Pure metric + aggregation functions for the variation-strategy sweep.

Headline metrics, per (strategy x defense x budget x seed):

  * efficiency  = distinct_findings / trials       -- bugs found per trial (the budget question)
  * recall      = distinct_findings / holes_avail  -- fraction of the discoverable surface found
  * dup_rate    = 1 - distinct_findings / raw_hits  -- budget wasted re-finding the same hole
  * obf_ops     = total obfuscator applications     -- a cheap compute-cost proxy

A FINDING is a distinct ``(payload_id, mechanism)`` pair (see defenses.py). ``holes_avail`` is the
union of mechanisms ANY strategy reaches at large budget over many seeds (empirical reachability),
so recall is "of everything discoverable, how much did this policy find at this budget."
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from benchmarks.variation_strategies.defenses import DEFENSES, features
from benchmarks.variation_strategies.payloads import Payload
from benchmarks.variation_strategies.strategies import Rendered, StrategyFn
from probe_engine.targets.agent_context import AgentContext


def _variant_rng(seed: int, probe_id: str, idx: int) -> random.Random:
    # Independent, reproducible stream per (seed, payload, variant). str() because Python 3.12
    # rejects tuple seeds — same adaptation generate.py uses.
    return random.Random(str((seed, probe_id, idx)))


def generate_cell(
    strategy: StrategyFn, payloads: list[Payload], ctx: AgentContext, budget: int, seed: int
) -> list[tuple[Payload, Rendered]]:
    """Produce ``budget`` variants per payload with one strategy (= variants-per-probe)."""
    out: list[tuple[Payload, Rendered]] = []
    for p in payloads:
        for idx in range(budget):
            rng = _variant_rng(seed, p.probe_id, idx)
            out.append((p, strategy(p.text, rng, ctx, idx, p.sensitivity)))
    return out


@dataclass
class CellScore:
    trials: int
    raw_hits: int
    findings: set[tuple[str, str]]
    obf_ops: int

    @property
    def distinct(self) -> int:
        return len(self.findings)

    @property
    def efficiency(self) -> float:
        return self.distinct / self.trials if self.trials else 0.0

    @property
    def dup_rate(self) -> float:
        return 1.0 - self.distinct / self.raw_hits if self.raw_hits else 0.0


def score_cell(rendered: list[tuple[Payload, Rendered]], defense_name: str) -> CellScore:
    defense = DEFENSES[defense_name]
    findings: set[tuple[str, str]] = set()
    raw_hits = 0
    obf_ops = 0
    for p, r in rendered:
        obf_ops += len(r.obf_chain)
        mech = defense(features(r))
        if mech is not None:
            raw_hits += 1
            findings.add((p.probe_id, mech))
    return CellScore(trials=len(rendered), raw_hits=raw_hits, findings=findings, obf_ops=obf_ops)


def reachable_holes(
    strategies: dict[str, StrategyFn],
    payloads: list[Payload],
    ctx: AgentContext,
    sweep_seeds: int = 20,
    sweep_budget: int = 64,
) -> dict[str, set[tuple[str, str]]]:
    """Empirical denominator for recall: union of findings ANY strategy reaches over many seeds at a
    large budget, per defense. This is what 'all discoverable holes' means for this payload set."""
    holes: dict[str, set[tuple[str, str]]] = {d: set() for d in DEFENSES}
    for strategy in strategies.values():
        for seed in range(sweep_seeds):
            rendered = generate_cell(strategy, payloads, ctx, sweep_budget, seed)
            for d in DEFENSES:
                holes[d] |= score_cell(rendered, d).findings
    return holes


def is_reproducible(
    strategy: StrategyFn, payloads: list[Payload], ctx: AgentContext, budget: int, seed: int
) -> bool:
    """Same (strategy, seed) -> identical rendered text multiset."""
    a = [r.text for _, r in generate_cell(strategy, payloads, ctx, budget, seed)]
    b = [r.text for _, r in generate_cell(strategy, payloads, ctx, budget, seed)]
    return a == b


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


def verdict(agg: dict) -> list[str]:
    """Turn the aggregated grid into plain-language conclusions.

    ``agg`` shape: agg[budget][defense][strategy] = {efficiency, recall, dup_rate, ...} (means)."""
    lines: list[str] = []
    budgets = sorted(agg)
    top = budgets[-1]
    grid = agg[top]
    strategies = sorted({s for d in grid.values() for s in d})

    lines.append(f"At the largest budget ({top} variants/payload):")
    wins = {s: 0 for s in strategies}
    for defense in sorted(grid):
        ranked = sorted(strategies, key=lambda s: grid[defense][s]["recall"], reverse=True)
        winner = ranked[0]
        wins[winner] += 1
        rec = {s: round(grid[defense][s]["recall"], 2) for s in strategies}
        lines.append(f"  - {defense}: best recall = {winner}  {rec}")

    lines.append("")
    # Aggregate (mean across defenses) efficiency + recall at top budget.
    for s in strategies:
        eff = mean([grid[d][s]["efficiency"] for d in grid])
        rec = mean([grid[d][s]["recall"] for d in grid])
        dup = mean([grid[d][s]["dup_rate"] for d in grid])
        lines.append(
            f"  {s:8s}  mean recall={rec:.2f}  mean efficiency={eff:.3f}  mean dup_rate={dup:.2f}  "
            f"defense-wins={wins[s]}"
        )

    # Rule-out: any strategy that wins no defense AND has lowest aggregate recall.
    losers = [s for s in strategies if wins[s] == 0]
    agg_recall = {s: mean([grid[d][s]["recall"] for d in grid]) for s in strategies}
    worst = min(agg_recall, key=lambda s: agg_recall[s])
    if worst in losers:
        lines.append("")
        lines.append(
            f"  => RULE-OUT: '{worst}' wins no defense regime and has the lowest aggregate recall."
        )
    # Robustness: highest minimum recall across defenses = best worst-case.
    minrec = {s: min(grid[d][s]["recall"] for d in grid) for s in strategies}
    robust = max(minrec, key=lambda s: minrec[s])
    lines.append(
        f"  => MOST ROBUST (best worst-case recall across defenses): '{robust}' "
        f"(min recall {minrec[robust]:.2f})."
    )
    return lines
