"""Full-surface fire check for the extended probe set.

Every NEW probe added in this integration must be able to FIRE end-to-end on the
offline `mock` tier when the target exposes the maximum attack surface: every non-conversation
canonical delivery channel, an acting principal + a seeded vulnerable backend, a seeded
lifecycle, and a tool inventory covering every capability the corpus targets.

This is the contract that a NEW probe is genuinely exercisable — not silently gated out (a blind
spot read as "robust") and not wired to an oracle the mock cannot satisfy. Under the always-fire
mock policy a reachable probe MUST score asr == 1.0; anything less is a BUG in the probe, its
oracle, or the mock wiring (fix those, never weaken this assertion).
"""

from pathlib import Path

import pytest

from probe_engine.corpus.loader import load_corpus
from probe_engine.domain.probe import CANONICAL_CHANNELS
from probe_engine.domain.run import (
    Principal,
    RunConfig,
    TargetConfig,
    Thresholds,
    ToolSpec,
)
from probe_engine.run.executor import run_probe
from probe_engine.run.selection import probe_applies
from probe_engine.targets.mock import MockPolicy

ROOT = Path(__file__).resolve().parents[2]

# The probes introduced by this integration. Each MUST fire under full surface.
NEW_PROBE_IDS = sorted(
    {
        "ask-to-translate-and-execute",
        "bot-config-exfiltration",
        "catastrophic-misuse",
        "ci-config-tamper",
        "citation-evidence-spoofing",
        "cross-session-leak",
        "cross-session-memory-campaign",
        "dependency-backdoor",
        "deploy-unreviewed-code",
        "generative-hallucination",
        "harmful-content-generation",
        "indirect-code-backdoor-via-ticket",
        "indirect-deploy-via-ticket",
        "indirect-injection-via-document",
        "indirect-injection-via-image",
        "insecure-output-markdown",
        "insecure-output-xss",
        "out-of-scope-action",
        "pii-enumeration",
        "privilege-enumeration",
        "rtbf-retention-consistency",
        "run-the-code-payload",
        "secrets-enumeration",
        "self-roundtrip-base64",
        "template-check-injection",
        "token-exhaustion",
        "tool-arg-command-injection",
        "tool-arg-ssrf",
        "unauthorized-deploy",
    }
)

# Every non-conversation canonical channel (message/history are universal and always present).
_INGESTION_CHANNELS = sorted(CANONICAL_CHANNELS - {"message", "history"})

# A tool inventory covering every capability the universal corpus targets, so no probe is gated
# out for a missing capability. One concrete tool per capability (name == capability is fine; the
# capability layer also maps a bare name to itself). `dangerous`/authz/stage args are set so the
# tool_called / authz / state oracles all have a tool to fire against.
_CAPABILITIES = [
    "send_message",
    "status_change",
    "run_command",
    "fetch_url",
    "record_write",
    "escalate",
    "read_file",
    "edit_file",
    "move_file",
    "read_memory",
    "write_memory",
    "read_reviews",
    "lookup_patient",
    "get_balance",
    "transfer",
    "schedule_transfer",
    "create_payment_mandate",
    "account_modify",
    # coding/deploy-agent threat pack capabilities (appended at the END so the deterministic
    # variant selection of every pre-existing probe stays byte-identical — see test_coding_deploy_probes).
    "code_exec",
    "deploy",
]


def _tool_inventory() -> list[ToolSpec]:
    inv: list[ToolSpec] = []
    for cap in _CAPABILITIES:
        spec = ToolSpec(name=cap, description=f"tool for {cap}", dangerous=True, capabilities=[cap])
        if cap == "status_change":
            inv.append(spec.model_copy(update={"stage_arg": "status"}))
        elif cap == "account_modify":
            inv.append(spec.model_copy(update={"resource_arg": "account", "authz_action": "modify"}))
        else:
            inv.append(spec)
    return inv


def _full_surface_run_config() -> RunConfig:
    target = TargetConfig(
        tier="mock",
        name="full-surface-mock",
        # every poisonable surface declared -> no channel-scoped probe is gated out
        channels=["message", "history", *_INGESTION_CHANNELS],
        # identity context (for authz probes) + a seeded vulnerable backend
        acting_principal=Principal(id="U_test", role="agent"),
        resource_owners={"RES_VICTIM": "U_other"},
        authz_default="allow",
        # a seeded lifecycle with a terminal-reject floor (for state-invariant probes)
        seed_stage="review",
        lifecycle_order=["rejected", "pending", "review", "approved"],
        lifecycle_floor="review",
        protected_snippets=["INTERNAL-SECRET-CRITERIA-DO-NOT-LEAK"],
    )
    return RunConfig(
        target=target,
        industry="any",
        tool_inventory=_tool_inventory(),
        n_variants=2,
        epochs=1,
        thresholds=Thresholds(),
        run_id="full-surface",
        timestamp="2026-06-24T00:00:00Z",
    )


@pytest.fixture(scope="module")
def corpus_by_id() -> dict:
    return {p.id: p for p in load_corpus(ROOT / "corpus" / "probes")}


@pytest.fixture(scope="module")
def full_surface() -> RunConfig:
    return _full_surface_run_config()


def test_every_new_probe_is_present(corpus_by_id):
    missing = [pid for pid in NEW_PROBE_IDS if pid not in corpus_by_id]
    assert not missing, f"new probe ids missing from corpus: {missing}"


def test_full_surface_selects_every_new_probe(corpus_by_id, full_surface):
    # Under the maximal surface, no new probe may be gated out (that would be a blind spot, not
    # a fired result). Selection must keep all of them.
    not_selected = [pid for pid in NEW_PROBE_IDS if not probe_applies(corpus_by_id[pid], full_surface)]
    assert not not_selected, f"new probes gated out under full surface (blind spot, not robust): {not_selected}"


@pytest.mark.parametrize("probe_id", NEW_PROBE_IDS)
def test_new_probe_fires_under_full_surface(probe_id, corpus_by_id, full_surface, tmp_path):
    probe = corpus_by_id[probe_id]
    evidence = run_probe(
        probe,
        full_surface,
        mock_policy=MockPolicy(rule="always"),
        seed=1,
        log_dir=str(tmp_path / "logs"),
    )
    assert evidence.n_errors == 0, f"{probe_id}: target errored ({evidence.n_errors} sample(s))"
    assert evidence.asr == 1.0, (
        f"{probe_id}: did not fire under full surface (asr={evidence.asr}); "
        f"this is a bug in the probe/oracle/mock wiring, not a robust agent"
    )
