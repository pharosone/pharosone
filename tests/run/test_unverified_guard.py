"""GUARD: an UNJUDGED false-positive-prone binary oracle (`prompt_leak` / `contains`) must NOT back
a confident fail NOR a robust pass. The executor downgrades such a probe's Evidence.status to
EvidenceStatus.UNVERIFIED (n_success / asr stay populated for transparency). A judged verdict, or a
non-FP-prone oracle, is unaffected.

Fully offline (mock tier, no judge_model resolves -> binary-only path), mirroring the existing
tests/run/test_judge_unavailable_wiring.py style. The batch judge is stubbed via
scoring.batch_judge.get_model so the JUDGED-path acceptance test never touches the network."""

import json
from types import SimpleNamespace

from probe_engine.corpus.loader import load_corpus
from probe_engine.domain.enums import EvidenceStatus
from probe_engine.domain.run import RunConfig, TargetConfig, Thresholds
from probe_engine.run.executor import run_probe

CORPUS = "corpus/probes"


def _probe(pid: str):
    return next(p for p in load_corpus(CORPUS) if p.id == pid)


def _rc(judge_model: str | None = None, protected: list[str] | None = None) -> RunConfig:
    return RunConfig(
        target=TargetConfig(
            tier="mock",
            judge_model=judge_model,
            protected_snippets=protected or [],
            channels=["message", "history"],
        ),
        n_variants=3,
        epochs=1,
        thresholds=Thresholds(asr_pass=0.05, target_asr=0.05),
        run_id="r",
        timestamp="t",
    )


def _always_hit_judge(*_a, **_k):
    """A judge model whose pass1 says 'yes' and pass2 marks every record a violation -> judge FAIL,
    proving the JUDGED path overrides the binary AND is NOT downgraded to UNVERIFIED."""
    async def generate(prompt):
        if "any_violation" in prompt:
            return SimpleNamespace(completion=json.dumps({"any_violation": True}))
        # pass2: mark a generous set of indices as confident violations
        viols = [{"index": i, "confidence": 1.0} for i in range(50)]
        return SimpleNamespace(completion=json.dumps({"violations": viols}))
    return SimpleNamespace(generate=generate)


# ---- (a) a prompt_leak / contains probe scored binary-only -> UNVERIFIED (not fail) ---------------

def test_prompt_leak_binary_only_is_unverified(tmp_path):
    ev = run_probe(_probe("extraction-system-prompt"), _rc(), log_dir=str(tmp_path / "l"))
    assert ev.status is EvidenceStatus.UNVERIFIED
    assert ev.status not in (EvidenceStatus.FAIL, EvidenceStatus.PASS)
    # n_success / asr are kept for transparency, not zeroed out.
    assert ev.n_trials > 0


def test_contains_binary_only_is_unverified(tmp_path):
    ev = run_probe(_probe("catastrophic-misuse"), _rc(), log_dir=str(tmp_path / "l"))
    assert ev.status is EvidenceStatus.UNVERIFIED
    assert ev.n_trials > 0


def test_prompt_leak_empty_protected_snippets_is_unverified(tmp_path):
    # prompt_leak with EMPTY protected_snippets has no real reference -> still UNVERIFIED.
    ev = run_probe(
        _probe("extraction-system-prompt"), _rc(protected=[]), log_dir=str(tmp_path / "l")
    )
    assert ev.status is EvidenceStatus.UNVERIFIED


# ---- (b) the SAME probe WITH a judge applied -> status reflects the judge verdict (not UNVERIFIED)-

def test_judged_prompt_leak_is_not_unverified(monkeypatch, tmp_path):
    monkeypatch.setattr("probe_engine.scoring.batch_judge.get_model", _always_hit_judge)
    ev = run_probe(
        _probe("extraction-system-prompt"),
        _rc(judge_model="openrouter/openai/gpt-5-mini"),
        log_dir=str(tmp_path / "l"),
    )
    # A judge ran -> the verdict is authoritative; the guard must NOT have downgraded it.
    assert ev.status is not EvidenceStatus.UNVERIFIED
    # here the stubbed judge marks every trial a violation -> a real FAIL verdict.
    assert ev.status is EvidenceStatus.FAIL


def test_judged_contains_is_not_unverified(monkeypatch, tmp_path):
    monkeypatch.setattr("probe_engine.scoring.batch_judge.get_model", _always_hit_judge)
    ev = run_probe(
        _probe("catastrophic-misuse"),
        _rc(judge_model="openrouter/openai/gpt-5-mini"),
        log_dir=str(tmp_path / "l"),
    )
    assert ev.status is not EvidenceStatus.UNVERIFIED


# ---- (c) an oracle NOT in the FP-prone set is unaffected by the guard ----------------------------

def test_output_pattern_oracle_unaffected_by_guard(tmp_path):
    # output_pattern is a real side-effect/marker oracle, never downgraded even unjudged.
    ev = run_probe(_probe("insecure-output-markdown"), _rc(), log_dir=str(tmp_path / "l"))
    assert ev.status is not EvidenceStatus.UNVERIFIED
    assert ev.status in (
        EvidenceStatus.PASS, EvidenceStatus.FAIL, EvidenceStatus.INSUFFICIENT_POWER,
        EvidenceStatus.NOT_RUN,
    )
