"""Opt-in resumability for a corpus run (B3).

A stopped run loses everything; re-running re-does expensive adaptive probes (~400s/trial). With
`resume=True`, `run_corpus` persists each probe's Evidence to `<out>/.checkpoint/<probe_id>.json`
keyed by a CONFIG HASH of only the inputs that affect a probe's RESULT — the probe's content plus
the depth/strategy of the run. On a later resumed run, a checkpoint whose config-hash matches is
loaded and the probe is SKIPPED (its persisted Evidence is reused, in order).

SECRETS NEVER TOUCH DISK (engine invariant): the config hash and the checkpoint file deliberately
exclude api_key, system prompts, and protected_snippets — they are not result-affecting inputs we
can persist. The Evidence we store already only carries what the engine writes for any report (it
may include the agent's own leaked reply in the transcript by design, exactly as today's reports do;
we add NO new secret)."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from probe_engine.domain.evidence import Evidence
from probe_engine.domain.probe import Probe
from probe_engine.domain.run import RunConfig

logger = logging.getLogger(__name__)

CHECKPOINT_DIR = ".checkpoint"


def _result_affecting_config(run_config: RunConfig, n_variants: int, epochs: int) -> dict:
    """The subset of run inputs that change a probe's RESULT — and NOTHING secret.

    Depth (n_variants*epochs as actually allocated for THIS probe) and the strategy knobs change the
    trials; tier/channels/tool declarations change which oracle fires and how. We explicitly do NOT
    include target.api_key (not a field), target.system_prompt, or target.protected_snippets — those
    are secrets and must never be hashed onto disk (invariant 2)."""
    t = run_config.target
    return {
        "tier": t.tier,
        "name": t.name,
        # Deployment identity that changes the RESULT (non-secret, safe to hash): endpoint (on the
        # bridge tier this points the run at a DIFFERENT agent), protocol (request shaping), and
        # provider (prefixes the model/attacker/judge slugs -> changes the effective model resolved).
        # Omitting these silently reused stale Evidence across targets/models on a resumed non-mock run.
        "endpoint": t.endpoint,
        "protocol": t.protocol,
        "provider": t.provider,
        "model": t.model,
        "attacker_model": t.attacker_model,
        "paraphrase_model": t.paraphrase_model,
        "judge_model": t.judge_model,
        "channels": list(t.channels),
        "acting_principal": t.acting_principal.model_dump() if t.acting_principal else None,
        "resource_owners": dict(t.resource_owners),
        "authz_default": t.authz_default,
        "seed_stage": t.seed_stage,
        "lifecycle_order": list(t.lifecycle_order),
        "lifecycle_floor": t.lifecycle_floor,
        "tool_inventory": [s.model_dump() for s in run_config.tool_inventory],
        "available_tools": list(run_config.available_tools),
        "languages": list(run_config.languages),
        "variation_strategy": run_config.variation_strategy,
        "thresholds": run_config.thresholds.model_dump(),
        "n_variants": n_variants,
        "epochs": epochs,
    }


def config_hash(probe: Probe, run_config: RunConfig, n_variants: int, epochs: int, seed: int) -> str:
    """Stable hash over the probe's content + the result-affecting (non-secret) run config + seed.

    Two runs that would produce the same Evidence share a hash; changing the probe YAML, the depth,
    the strategy, or the seed busts it. Secrets are excluded by construction (see above)."""
    payload = {
        "probe": probe.model_dump(mode="json"),
        "config": _result_affecting_config(run_config, n_variants, epochs),
        "seed": seed,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _path(out_dir: str, probe_id: str) -> Path:
    # probe ids can carry '@stage' (lifecycle) and '/'; keep the file name filesystem-safe.
    safe = probe_id.replace("/", "_").replace("@", "__at__")
    return Path(out_dir) / CHECKPOINT_DIR / f"{safe}.json"


def load(out_dir: str, probe_id: str, expected_hash: str) -> Evidence | None:
    """Return the persisted Evidence for `probe_id` IFF a checkpoint exists AND its config-hash
    matches `expected_hash`; else None (so the probe re-runs). A stale/mismatched/corrupt checkpoint
    is ignored, never trusted."""
    path = _path(out_dir, probe_id)
    if not path.is_file():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # corrupt checkpoint -> re-run, don't crash
        logger.warning("ignoring unreadable checkpoint %s: %r", path, e)
        return None
    if obj.get("config_hash") != expected_hash:
        logger.info("checkpoint config-hash mismatch for %s -> re-running", probe_id)
        return None
    try:
        return Evidence.model_validate(obj["evidence"])
    except Exception as e:
        logger.warning("ignoring malformed checkpoint evidence %s: %r", path, e)
        return None


def save(out_dir: str, probe_id: str, config_hash_: str, evidence: Evidence) -> None:
    """Persist `evidence` for `probe_id` under `<out>/.checkpoint/`, stamped with its config-hash.

    Only the engine's own Evidence is written — no api_key / system prompt / protected_snippets are
    serialized here (they are not on Evidence and not in the config hash)."""
    path = _path(out_dir, probe_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"config_hash": config_hash_, "evidence": evidence.model_dump(mode="json")}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
