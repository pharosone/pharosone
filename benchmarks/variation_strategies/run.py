"""Run the variation-strategy benchmark and print a data-driven verdict.

    uv run python -m benchmarks.variation_strategies.run
    uv run python -m benchmarks.variation_strategies.run --budgets 8,32,128 --seeds 5 --payloads 20 \
        --out reports/variation_bench.json

Offline, deterministic, no API keys. See README.md for what it does and does NOT prove.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from benchmarks.variation_strategies.defenses import DEFENSES
from benchmarks.variation_strategies.metrics import (
    generate_cell,
    is_reproducible,
    mean,
    reachable_holes,
    score_cell,
    stdev,
    verdict,
)
from benchmarks.variation_strategies.payloads import default_context, load_payloads
from benchmarks.variation_strategies.strategies import STRATEGIES


def run(budgets: list[int], seeds: int, payload_limit: int | None) -> dict:
    payloads = load_payloads(limit=payload_limit)
    ctx = default_context()
    holes = reachable_holes(STRATEGIES, payloads, ctx)
    hole_counts = {d: len(h) for d, h in holes.items()}

    # agg[budget][defense][strategy] = {metric: mean, ...}
    agg: dict[int, dict[str, dict[str, dict[str, float]]]] = {}
    for budget in budgets:
        agg[budget] = {d: {} for d in DEFENSES}
        for sname, sfn in STRATEGIES.items():
            # Generate each (seed) cell ONCE, score against every defense.
            per_seed: dict[str, list[dict[str, float]]] = {d: [] for d in DEFENSES}
            for seed in range(seeds):
                rendered = generate_cell(sfn, payloads, ctx, budget, seed)
                for d in DEFENSES:
                    cs = score_cell(rendered, d)
                    denom = hole_counts[d] or 1
                    per_seed[d].append(
                        {
                            "efficiency": cs.efficiency,
                            "recall": cs.distinct / denom,
                            "dup_rate": cs.dup_rate,
                            "raw_hits": float(cs.raw_hits),
                            "distinct": float(cs.distinct),
                            "obf_ops": float(cs.obf_ops),
                            "trials": float(cs.trials),
                        }
                    )
            for d in DEFENSES:
                rows = per_seed[d]
                agg[budget][d][sname] = {
                    k: mean([r[k] for r in rows]) for k in rows[0]
                } | {"recall_std": stdev([r["recall"] for r in rows])}

    reproducible = {
        s: is_reproducible(fn, payloads, ctx, budget=min(budgets), seed=0)
        for s, fn in STRATEGIES.items()
    }

    return {
        "config": {
            "budgets": budgets,
            "seeds": seeds,
            "n_payloads": len(payloads),
            "strategies": list(STRATEGIES),
            "defenses": list(DEFENSES),
        },
        "holes_available": hole_counts,
        "reproducible": reproducible,
        "grid": agg,
        "verdict": verdict(agg),
    }


def _print_report(rep: dict) -> None:
    cfg = rep["config"]
    print("=" * 92)
    print("VARIATION-STRATEGY BENCHMARK  (synthetic defense panel — see README for caveats)")
    print("=" * 92)
    print(
        f"payloads={cfg['n_payloads']}  seeds={cfg['seeds']}  budgets={cfg['budgets']}  "
        f"strategies={cfg['strategies']}"
    )
    print(f"discoverable holes per defense: {rep['holes_available']}")
    print(f"reproducible (same seed -> same output): {rep['reproducible']}")

    strategies = cfg["strategies"]
    for budget in cfg["budgets"]:
        print("\n" + "-" * 92)
        print(f"BUDGET = {budget} variants/payload   (metric shown: RECALL = found / discoverable)")
        print("-" * 92)
        header = f"{'defense':22s}" + "".join(f"{s:>14s}" for s in strategies)
        print(header)
        grid = rep["grid"][budget] if isinstance(next(iter(rep["grid"])), int) else rep["grid"][str(budget)]
        for d in cfg["defenses"]:
            row = f"{d:22s}"
            for s in strategies:
                row += f"{grid[d][s]['recall']:>14.2f}"
            print(row)
        # dup_rate row summary (mean across defenses)
        dup = {s: mean([grid[d][s]["dup_rate"] for d in cfg["defenses"]]) for s in strategies}
        print(f"{'~mean dup_rate':22s}" + "".join(f"{dup[s]:>14.2f}" for s in strategies))

    print("\n" + "=" * 92)
    print("VERDICT")
    print("=" * 92)
    for line in rep["verdict"]:
        print(line)


def main() -> None:
    ap = argparse.ArgumentParser(description="Variation-strategy benchmark (offline).")
    ap.add_argument("--budgets", default="8,32,128", help="comma-separated variants/payload")
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--payloads", type=int, default=20, help="cap on payloads (0 = all)")
    ap.add_argument("--out", type=str, default="", help="optional JSON output path")
    args = ap.parse_args()

    budgets = [int(b) for b in args.budgets.split(",") if b.strip()]
    rep = run(budgets, args.seeds, args.payloads or None)
    _print_report(rep)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, indent=2, default=lambda o: sorted(o) if isinstance(o, set) else str(o)))
        print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
