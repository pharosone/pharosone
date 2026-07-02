"""LIVE calibration tier — run the same variation policies against a REAL model agent.

The offline sweep (``run.py``) compares the four policies against a panel of *synthetic* defenses.
Those defenses are hypotheses. This module grounds them: it drives each policy through the **real**
engine pipeline (real model-tier agent, real oracle, real judge, real Wilson CI) against one real
agent profile, then reports which synthetic defense archetype that real agent most resembles — so you
know which synthetic predictions to trust before changing any architecture.

HOW IT STAYS FAITHFUL: it does not re-implement anything. Each policy is injected at the engine's own
variation seam (``executor._build_attack_mutator``) via a restored monkeypatch, and the unchanged
``run_corpus`` does the rest. The ONLY thing that varies across the four runs is the selection policy.

COST + SECRETS (load-bearing):
  * This calls a real LLM — it costs money and time. It is OPT-IN: without ``--yes`` it does a fully
    OFFLINE dry run (load profile, select probes, print a cost estimate) and exits without any model
    call. Run the dry run first.
  * The API key is read from the environment ONLY (``PE_CLIENT_LLM_KEY`` or ``OPENROUTER_API_KEY``) —
    never from argv, never prompted. It stays in memory (engine guarantee). If unset, ``--yes`` aborts.

    # 1. dry run (offline, no key, no spend) — see the plan + cost estimate:
    uv run python -m benchmarks.variation_strategies.live \
        --profile configs/profiles/model-tier-example.yaml --probes 3 --budget 6

    # 2. real run (your key in env), small by default:
    PE_CLIENT_LLM_KEY=... uv run python -m benchmarks.variation_strategies.live \
        --profile configs/profiles/model-tier-example.yaml --probes 3 --budget 6 --yes \
        --bench reports/variation_bench.json --out reports/variation_live.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

import probe_engine.run.executor as executor
from probe_engine.config.profile import load_profile, run_config_from_profile
from probe_engine.corpus.loader import load_corpus
from probe_engine.domain.run import RunConfig
from probe_engine.run.selection import select_probes
from probe_engine.scoring.statistics import wilson_ci
from probe_engine.targets.agent_context import build_agent_context
from probe_engine.variation.llm_paraphrase import make_llm_attack_mutator

from benchmarks.variation_strategies.payloads import _SENSITIVITY
from benchmarks.variation_strategies.strategies import STRATEGIES

# The optional 5th arm: model-generated variation (the direct opponent of the static policies). It is
# NOT in STRATEGIES (which must stay deterministic/offline for the synthetic sweep in run.py) — it is
# built on the engine's own ``make_llm_attack_mutator`` and injected via the attack_mutator param.
LLM_POLICY = "llm"

# Rough per-probe call multiplier by scenario (single message vs multi-turn chain vs adaptive loop).
_TURN_COST = {"single_turn": 1, "chain": 3, "adaptive": 6}


def _scenario_cost(probe) -> int:
    kind = probe.scenario.type.value
    if kind == "chain":
        return max(1, len(probe.scenario.turns))
    if kind == "adaptive":
        return probe.scenario.max_turns or _TURN_COST["adaptive"]
    return 1


def _make_mutator(strategy_fn, ctx, sensitivity: str, seed: int):
    """Adapt a benchmark strategy ``(text, rng, ctx, idx, sensitivity)`` to the engine's variation
    mutator shape ``(text, lang, index) -> str``. The rng is seeded per (seed, payload, index) so the
    run is reproducible."""
    def mutate(text: str, lang: str, index: int) -> str:
        rng = random.Random(str((seed, text[:32], index)))
        return strategy_fn(text, rng, ctx, index, sensitivity).text

    return mutate


def _strategy_builder(strategy_fn, seed: int):
    """A variation-mutator BUILDER matching ``_build_attack_mutator``'s shape
    ``(probe, run_config, api_key, context) -> ((text,lang,idx)->str)``. Passed to ``run_corpus`` as
    ``attack_mutator`` (B) so a policy is injected WITHOUT the process-global monkeypatch — which is
    what makes concurrent multi-policy runs race-free."""
    def build(probe, run_config, api_key, context):
        ctx = context or build_agent_context(run_config)
        oracle = probe.evaluation.binary.oracle if probe.evaluation.binary else "contains"
        sens = _SENSITIVITY.get(oracle, "med")
        return _make_mutator(strategy_fn, ctx, sens, seed)

    return build


@dataclass
class GenStats:
    """Generation-side accounting for the ``llm`` arm (decision: a silently-all-fallback llm arm must
    be VISIBLE, never masquerade as the deterministic policy).

    Records, across every ``mutate`` call in one policy run: how many calls actually used a real LLM
    rewrite vs fell back to the deterministic ``diversify`` (model error / refusal / empty), and the
    generation model's token usage (for cost normalization — the 4 static arms have ZERO generation
    cost; only this arm adds it). ``record`` is the ``on_call`` hook handed to
    ``make_llm_attack_mutator``."""

    n_calls: int = 0
    n_llm_used: int = 0
    n_fallback: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def record(self, used_llm: bool, prompt_tokens: int, completion_tokens: int) -> None:
        self.n_calls += 1
        if used_llm:
            self.n_llm_used += 1
        else:
            self.n_fallback += 1
        self.prompt_tokens += int(prompt_tokens or 0)
        self.completion_tokens += int(completion_tokens or 0)

    @property
    def llm_fraction(self) -> float:
        """Fraction of mutate calls that used a REAL LLM rewrite (1.0 = no fallbacks; 0.0 = the arm
        silently degraded to the deterministic diversifier on every call)."""
        return self.n_llm_used / self.n_calls if self.n_calls else 0.0

    def as_dict(self) -> dict:
        return {
            "n_calls": self.n_calls,
            "n_llm_used": self.n_llm_used,
            "n_fallback": self.n_fallback,
            "llm_fraction": self.llm_fraction,
            "gen_prompt_tokens": self.prompt_tokens,
            "gen_completion_tokens": self.completion_tokens,
            "gen_total_tokens": self.prompt_tokens + self.completion_tokens,
        }


def _llm_strategy_builder(model_id: str, stats: GenStats):
    """A variation-mutator BUILDER (same shape as ``executor._build_attack_mutator``) for the ``llm``
    arm. It wires ``make_llm_attack_mutator`` (generation model = ``model_id``) with an ``on_call``
    hook that records GENERATION token usage + real-rewrite-vs-fallback counts into ``stats``.

    Passed to ``run_corpus`` as ``attack_mutator`` (B) so the llm policy is injected WITHOUT the
    process-global monkeypatch — it runs in the same race-free, parallelizable path as the static
    arms. ``stats`` is per-process (each ProcessPool worker owns its own), so the accumulation never
    races; the worker returns its counts in the result."""
    def build(probe, run_config, api_key, context):
        ctx = context or build_agent_context(run_config)
        return make_llm_attack_mutator(model_id, api_key, context=ctx, on_call=stats.record)

    return build


# ----------------------------------------------------------------------------------------------
# FROZEN-PACK REPLAY for the llm arm. Live model-generated variation INSIDE the timed run crawls
# under provider rate limits (thousands of GLM calls on top of the GLM judge -> effective hang).
# Instead the variants are pre-generated ONCE into a frozen pack (see the pregen runner) and the
# llm arm REPLAYS them statically here — zero live generation calls during the run, deterministic by
# index, seed-independent. An UNCOVERED payload returns "" so generate_variants falls back to the
# deterministic context-bound diversify; that fallback is COUNTED (never a silent gap), exactly as a
# silently-all-fallback live arm is made visible by GenStats.
# ----------------------------------------------------------------------------------------------
def pack_key(text: str) -> str:
    """Pack key for a payload: sha1 of its UTF-8 bytes (matches the pregen + replay step)."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def load_attack_pack(pack_path: str | Path) -> dict[str, list[str]]:
    """Load a frozen attack pack JSONL (``{"key": sha1(payload), "variants": [...]}``) into a
    ``key -> variants`` map. The file is append-only / resumable, so a key may recur: the LAST
    non-empty ``variants`` wins (a resumed pregen's successful retry overrides an earlier empty
    line). Keys whose only entries are empty stay mapped to ``[]`` (treated as uncovered at replay)."""
    pack: dict[str, list[str]] = {}
    with open(pack_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            key = d.get("key")
            if key is None:
                continue
            variants = [str(v) for v in (d.get("variants") or [])]
            # A non-empty list always wins; an empty list only sets a key not seen yet.
            if variants or key not in pack:
                pack[key] = variants
    return pack


@dataclass
class ReplayStats:
    """Coverage accounting for the REPLAY (frozen-pack) llm arm — the replay twin of ``GenStats``.

    Per ``mutate`` call it records whether the pack COVERED the payload (a frozen variant was served)
    or the payload was UNCOVERED and fell back to the deterministic ``diversify``. An all-uncovered
    arm silently degrades to the deterministic policy, so this makes coverage VISIBLE and the gap
    never silent — the same honesty guarantee ``GenStats`` gives the live-generation arm."""

    n_calls: int = 0
    n_covered: int = 0
    n_uncovered: int = 0
    uncovered_keys: set[str] = field(default_factory=set)

    def record(self, covered: bool, key: str) -> None:
        self.n_calls += 1
        if covered:
            self.n_covered += 1
        else:
            self.n_uncovered += 1
            self.uncovered_keys.add(key)

    @property
    def covered_fraction(self) -> float:
        """Fraction of mutate calls served from the pack (1.0 = full coverage; 0.0 = the arm silently
        degraded to the deterministic diversifier on every call)."""
        return self.n_covered / self.n_calls if self.n_calls else 0.0

    def as_dict(self) -> dict:
        return {
            "n_calls": self.n_calls,
            "n_covered": self.n_covered,
            "n_uncovered": self.n_uncovered,
            "covered_fraction": self.covered_fraction,
            "n_uncovered_payloads": len(self.uncovered_keys),
            "uncovered_payloads": sorted(self.uncovered_keys),
        }


def _replay_strategy_builder(pack: dict[str, list[str]], stats: ReplayStats):
    """A variation-mutator BUILDER (same shape as ``executor._build_attack_mutator``) that REPLAYS a
    frozen pack instead of constructing or calling any generation model.

    ``mutate(text, lang, index)`` keys on ``pack_key(text)`` and serves ``variants[index % len]`` —
    deterministic by index and seed-independent, so one pack is shared across seeds AND prompt
    variants. An UNCOVERED payload returns ``""`` so ``generate_variants`` falls back to the
    deterministic ``diversify``; every call is recorded in ``stats`` (covered vs uncovered). No
    monkeypatch and no live model — safe under the process-pool parallelism of the 5-arm run."""

    def build(probe, run_config, api_key, context):
        def mutate(text: str, lang: str = "en", index: int = 0) -> str:
            if not text:
                return ""
            key = pack_key(text)
            variants = pack.get(key)
            if variants:
                stats.record(True, key)
                return variants[index % len(variants)]
            stats.record(False, key)
            return ""  # uncovered -> generate_variants uses the deterministic diversify (counted)

        return mutate

    return build


def _summarize(acc: dict, evidence) -> dict:
    per_probe = {
        pid: {
            "asr": s["n_success"] / s["n_trials"] if s["n_trials"] else 0.0,
            "n_success": s["n_success"],
            "n_trials": s["n_trials"],
            "status": s["statuses"][0] if len(set(s["statuses"])) == 1 else "mixed",
        }
        for pid, s in acc.items()
    }
    total_succ = sum(p["n_success"] for p in per_probe.values())
    total_trials = sum(p["n_trials"] for p in per_probe.values()) or 1
    breached = sum(1 for p in per_probe.values() if p["n_success"] > 0)
    return {
        "per_probe": per_probe,
        "pooled_asr": total_succ / total_trials,
        "n_success": total_succ,
        "probes_breached": breached,
        "coverage": breached / len(per_probe) if per_probe else 0.0,
        "total_trials": total_trials,
        "blind_spots": list(getattr(evidence, "blind_spots", []) or []),
    }


def _run_policy(strategy_name, strategy_fn, probes, rc: RunConfig, api_key, seeds, *, quiet=False) -> dict:
    """Run ONE policy across all seeds (in THIS process), pooling successes/trials so the ASR is over
    the whole multi-seed sample. Injects the policy via the ``attack_mutator`` param (no monkeypatch)."""
    acc: dict[str, dict] = {}
    last = None
    for seed in seeds:
        def progress(phase, i, total, probe, ev, _seed=seed):
            if phase == "done" and ev is not None:
                print(f"    [{strategy_name} s{_seed}] {i}/{total} {ev.probe_id}: "
                      f"ASR={ev.asr:.1%} [{ev.status.value}]", flush=True)

        last = executor.run_corpus(
            probes, rc, seed=seed, api_key=api_key,
            attack_mutator=_strategy_builder(strategy_fn, seed),
            progress=(None if quiet else progress),
        )
        for ev in last:
            slot = acc.setdefault(ev.probe_id, {"n_success": 0, "n_trials": 0, "statuses": []})
            slot["n_success"] += ev.n_success
            slot["n_trials"] += ev.n_trials
            slot["statuses"].append(ev.status.value)
    return _summarize(acc, last)


def _policy_worker(payload: dict):
    """Top-level (picklable) ProcessPool worker (B): rebuild config/probes in THIS process and run one
    policy across all seeds. The API key is read from this process's inherited env — never pickled.
    Each worker caps its own inspect concurrency so N workers share the provider rate budget."""
    os.environ["INSPECT_EVAL_MAX_CONNECTIONS"] = str(payload["max_connections"])
    api_key = os.environ.get("PE_CLIENT_LLM_KEY") or os.environ.get("OPENROUTER_API_KEY")
    profile = load_profile(payload["profile"])
    rc = run_config_from_profile(profile, run_id="variation-live", timestamp="live").model_copy(
        update={"n_variants": payload["budget"], "epochs": payload["epochs"], "fail_fast": False}
    )
    by_id = {p.id: p for p in load_corpus(payload["corpus"])}
    probes = [by_id[pid] for pid in payload["probe_ids"] if pid in by_id]
    fn = STRATEGIES[payload["policy"]]
    return payload["policy"], _run_policy(
        payload["policy"], fn, probes, rc, api_key, payload["seeds"], quiet=True
    )


def _screen(strategies, probes, rc: RunConfig, api_key: str, screen_budget: int) -> set[str]:
    """Quick 1-seed pass at a small budget; return the set of probe ids breached by ANY strategy.
    Used to drop always-0% probes from the (expensive) main sweep — see (a)."""
    rc_s = rc.model_copy(update={"n_variants": screen_budget, "epochs": 1})
    breached: set[str] = set()
    for fn in strategies.values():
        evidence = executor.run_corpus(
            probes, rc_s, seed=0, api_key=api_key, attack_mutator=_strategy_builder(fn, 0)
        )
        for ev in evidence:
            if ev.n_success > 0:
                breached.add(ev.probe_id)
    return breached


def _kendall_tau(order_scores: dict[str, float], ref_scores: dict[str, float]) -> float:
    """Rank concordance over the common strategy set, in [-1, 1]. 1 = identical ordering."""
    keys = [k for k in order_scores if k in ref_scores]
    if len(keys) < 2:
        return 0.0
    con = dis = 0
    for a, b in combinations(keys, 2):
        s = (order_scores[a] - order_scores[b]) * (ref_scores[a] - ref_scores[b])
        if s > 0:
            con += 1
        elif s < 0:
            dis += 1
    total = con + dis
    return (con - dis) / total if total else 0.0


def _calibrate(live_asr: dict[str, float], bench_path: Path, budget: int) -> dict:
    """Compare the live strategy ranking to each synthetic defense's ranking; the best-matching
    defense is the archetype the real agent most resembles."""
    bench = json.loads(bench_path.read_text())
    grid = bench["grid"]
    budgets = sorted(int(b) for b in grid)
    nearest = min(budgets, key=lambda b: abs(b - budget))
    defense_grid = grid[str(nearest)] if str(nearest) in grid else grid[nearest]
    taus = {}
    for defense, by_strategy in defense_grid.items():
        ref = {s: by_strategy[s]["recall"] for s in by_strategy}
        taus[defense] = _kendall_tau(live_asr, ref)
    best = max(taus, key=lambda d: taus[d]) if taus else None
    return {"nearest_budget": nearest, "tau_per_defense": taus, "best_match": best}


def run(args) -> dict:
    profile = load_profile(args.profile)
    rc = run_config_from_profile(profile, run_id="variation-live", timestamp="live")
    rc = rc.model_copy(update={"n_variants": args.budget, "epochs": args.epochs, "fail_fast": False})

    all_probes = load_corpus(args.corpus)
    selected = select_probes(all_probes, rc)
    # Cheapest scenarios first so the default subset is the least expensive to run live.
    selected.sort(key=_scenario_cost)
    if not args.include_adaptive:
        selected = [p for p in selected if p.scenario.type.value != "adaptive"] or selected
    probes = selected[: args.probes]

    strategies = {k: v for k, v in STRATEGIES.items() if not args.strategies or k in args.strategies}
    seeds = list(range(args.seed, args.seed + args.seeds))

    est_trials = (
        sum(_scenario_cost(p) * args.budget * args.epochs for p in probes)
        * len(strategies)
        * len(seeds)
    )
    plan = {
        "profile": str(args.profile),
        "tier": rc.target.tier,
        "model": rc.target.model,
        "judge_model": rc.target.judge_model,
        "selected_probes": [p.id for p in probes],
        "n_selected_total": len(selected),
        "strategies": list(strategies),
        "budget": args.budget,
        "epochs": args.epochs,
        "seeds": seeds,
        "max_connections": args.max_connections,
        "workers": args.workers,
        "screen_budget": args.screen_budget,
        "estimated_model_calls": est_trials,
    }

    print("=" * 88)
    print("LIVE CALIBRATION — variation strategies vs a REAL agent")
    print("=" * 88)
    for k, v in plan.items():
        print(f"  {k}: {v}")

    if not args.yes:
        print("\nDRY RUN (offline). No model was called, nothing was spent.")
        print("Re-run with --yes (and PE_CLIENT_LLM_KEY / OPENROUTER_API_KEY in env) to execute.")
        return {"plan": plan, "dry_run": True}

    api_key = os.environ.get("PE_CLIENT_LLM_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit(
            "no API key in env — set PE_CLIENT_LLM_KEY or OPENROUTER_API_KEY (never pass keys on argv)."
        )

    # Concurrency for the MAIN-process work (screening + the sequential path). The parallel workers
    # set their own (split) value. With A (parallel judge chunks) + B (policies run in separate
    # processes via the attack_mutator param — no global monkeypatch), the run is no longer serialized.
    os.environ["INSPECT_EVAL_MAX_CONNECTIONS"] = str(args.max_connections)

    # (a) screen out always-0% probes so the multi-seed sweep doesn't burn budget on them.
    if args.screen_budget > 0 and len(probes) > 1:
        print(f"\nscreening {len(probes)} probes @ budget {args.screen_budget} (1 seed) to drop always-0% ...")
        breached = _screen(strategies, probes, rc, api_key, args.screen_budget)
        dropped = [p.id for p in probes if p.id not in breached]
        probes = [p for p in probes if p.id in breached] or probes
        if dropped:
            # SURFACE, never silently drop: these are excluded to save budget, NOT proven robust.
            print(f"  screened OUT (0 hits in screening — excluded to save budget, NOT proven robust): {dropped}")
        print(f"  kept {len(probes)} breachable: {[p.id for p in probes]}")

    workers = max(1, min(args.workers, len(strategies)))
    results: dict[str, dict] = {}
    if workers > 1:
        # B: one policy per worker PROCESS (own globals -> the attack_mutator param injects the policy
        # with no shared state). Split the connection budget so N workers don't blow the rate limit.
        per_conn = max(4, args.max_connections // workers)
        print(f"\nrunning {len(strategies)} policies IN PARALLEL ({workers} workers x {per_conn} conn) "
              f"x {len(probes)} probes x {len(seeds)} seeds ...\n", flush=True)
        payloads = [
            {
                "policy": name, "profile": str(args.profile), "corpus": args.corpus,
                "probe_ids": [p.id for p in probes], "budget": args.budget, "epochs": args.epochs,
                "seeds": seeds, "max_connections": per_conn,
            }
            for name in strategies
        ]
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_policy_worker, p): p["policy"] for p in payloads}
            for fut in as_completed(futs):
                name, res = fut.result()
                results[name] = res
                print(f"  -- done: {name}  pooled_asr={res['pooled_asr']:.1%}  "
                      f"breached={res['probes_breached']}/{len(probes)}", flush=True)
    else:
        print(f"\nrunning {len(strategies)} strategies x {len(probes)} probes x {len(seeds)} seeds ...\n")
        for name, fn in strategies.items():
            print(f"  -- strategy: {name}")
            results[name] = _run_policy(name, fn, probes, rc, api_key, seeds)

    live_asr = {s: results[s]["pooled_asr"] for s in results}
    report = {"plan": plan, "dry_run": False, "results": results, "live_pooled_asr": live_asr}

    if args.bench and Path(args.bench).exists():
        report["calibration"] = _calibrate(live_asr, Path(args.bench), args.budget)

    _print_results(report)
    return report


def _print_results(rep: dict) -> None:
    print("\n" + "=" * 88)
    print("LIVE RESULTS  (pooled ASR = total successes / total trials, across the probe set)")
    print("=" * 88)
    res = rep["results"]
    strategies = list(res)
    print(f"{'strategy':16s}{'pooled_asr':>11s}{'95% CI (Wilson)':>20s}{'breached':>10s}{'trials':>9s}")
    ci = {}
    for s in strategies:
        r = res[s]
        lo, hi = wilson_ci(r["n_success"], r["total_trials"])
        ci[s] = (lo, hi)
        band = f"[{lo:.1%}, {hi:.1%}]"
        print(f"{s:16s}{r['pooled_asr']:>11.1%}{band:>20s}{r['probes_breached']:>10d}{r['total_trials']:>9d}")

    ranked = sorted(strategies, key=lambda s: res[s]["pooled_asr"], reverse=True)
    print(f"\n  live ranking (strongest attacker first): {ranked}")

    # Signal-vs-noise verdict. curated and tuple-curated send IDENTICAL inputs (proven), so any gap
    # between them is pure target/judge nondeterminism — the empirical noise floor.
    asr = {s: res[s]["pooled_asr"] for s in strategies}
    if "curated" in asr and "tuple-curated" in asr:
        floor = abs(asr["curated"] - asr["tuple-curated"]) * 100
        print(f"  noise floor (curated vs tuple-curated, identical inputs) = {floor:.1f} pp")
    distinct = [
        (a, b)
        for a, b in combinations(strategies, 2)
        if ci[a][1] < ci[b][0] or ci[b][1] < ci[a][0]  # non-overlapping 95% CIs
    ]
    if distinct:
        pairs = ", ".join(f"{a}≠{b}" for a, b in distinct)
        print(f"  CI-distinguishable pairs (non-overlapping 95% CI): {pairs}")
    else:
        print("  => no pair is CI-distinguishable: all policies are WITHIN NOISE at this sample size.")

    cal = rep.get("calibration")
    if cal:
        print(f"\n  calibration vs synthetic sweep (nearest budget {cal['nearest_budget']}):")
        for d, t in sorted(cal["tau_per_defense"].items(), key=lambda kv: kv[1], reverse=True):
            print(f"    {d:22s} rank-concordance tau = {t:+.2f}")
        print(f"\n  => the real agent most resembles the '{cal['best_match']}' archetype.")
        print("     (trust that archetype's offline predictions; recalibrate defenses if tau is low.)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Live calibration of variation strategies (opt-in, costs money).")
    ap.add_argument("--profile", required=True, help="run profile (model or bridge tier)")
    ap.add_argument("--corpus", default="corpus/probes")
    ap.add_argument("--probes", type=int, default=3, help="number of (cheapest) selected probes to run")
    ap.add_argument("--budget", type=int, default=6, help="variants/probe (n_variants)")
    ap.add_argument("--epochs", type=int, default=1)
    ap.add_argument("--seed", type=int, default=0, help="base seed")
    ap.add_argument("--seeds", type=int, default=1, help="number of seeds to pool (variance control)")
    ap.add_argument("--strategies", default="", help="comma-separated subset (default: all four)")
    ap.add_argument("--max-connections", type=int, default=24, help="inspect concurrent API calls (speed lever)")
    ap.add_argument("--workers", type=int, default=4,
                    help="parallel policy workers (processes); 1 = sequential. Connection budget is split across them")
    ap.add_argument("--screen-budget", type=int, default=0,
                    help=">0: quick 1-seed screen at this budget to drop always-0% probes before the sweep")
    ap.add_argument("--include-adaptive", action="store_true", help="allow expensive adaptive probes")
    ap.add_argument("--bench", default="reports/variation_bench.json", help="synthetic sweep JSON for calibration")
    ap.add_argument("--out", default="", help="optional JSON output path")
    ap.add_argument("--yes", action="store_true", help="actually call the model (else dry run)")
    args = ap.parse_args()
    args.strategies = [s for s in args.strategies.split(",") if s.strip()]

    rep = run(args)

    if args.out and not rep.get("dry_run"):
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, indent=2))
        print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
