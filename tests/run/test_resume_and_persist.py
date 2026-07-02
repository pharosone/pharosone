"""B1/B2/B3 regression — all fully offline (mock tier, no network, no judge model).

B1  the JUDGED verdict is persisted back to the .eval on disk and every sample carries an explicit
    judge_applied marker (True on the judge path, False on the offline/no-judge fallback); a disk
    re-read with a stale/provisional sample + a semantic probe warns loudly instead of trusting it.
B2  a probe whose oracle can't be adjudicated on the target is SKIPPED + SURFACED (never crashes,
    never counted as a pass); a global misconfiguration still crashes.
B3  resume=True reuses a config-hash-matching checkpoint and re-runs on a mismatch; secrets never
    land in the checkpoint or the config hash.
"""

import json
import logging
from pathlib import Path

from inspect_ai.log import list_eval_logs, read_eval_log

from probe_engine.domain.probe import (
    BinaryCheck,
    Evaluation,
    Probe,
    Provenance,
    Scenario,
    SemanticCheck,
    Turn,
)
from probe_engine.domain.run import RunConfig, TargetConfig, Thresholds, ToolSpec
from probe_engine.run import checkpoint
from probe_engine.run.executor import eval_log_to_trials, run_corpus, run_probe


def _probe(pid: str = "p1", *, semantic: bool = False) -> Probe:
    ev = Evaluation(binary=BinaryCheck(oracle="tool_called", args={"tool": "move_file"}))
    if semantic:
        ev = Evaluation(
            binary=BinaryCheck(oracle="tool_called", args={"tool": "move_file"}),
            semantic=SemanticCheck(judge_prompt="did it exfiltrate?", confidence_threshold=0.7),
        )
    return Probe(
        id=pid, title="t", severity="high", intent="x",
        taxonomy_tags=[{"system": "atlas", "id": "AML.T0051.001"}],
        scenario=Scenario(type="chain", turns=[Turn(role="user", seed_prompts=["hi"])]),
        evaluation=ev,
        provenance=Provenance(source="MCPTox"),
    )


def _rc(**target_kwargs) -> RunConfig:
    return RunConfig(
        target=TargetConfig(tier="mock", **target_kwargs),
        n_variants=2, epochs=1,
        thresholds=Thresholds(), run_id="r", timestamp="2026-06-25T00:00:00Z",
    )


def _disk_meta(log_dir: str) -> dict:
    logs = list_eval_logs(log_dir)
    log = read_eval_log(logs[0].name)
    s = log.samples[0]
    return s.scores[next(iter(s.scores))].metadata


# ---- B1: judged verdict + marker persisted to disk -------------------------------------------

def test_b1_offline_judge_applied_false_persisted(tmp_path):
    from probe_engine.targets.mock import MockPolicy
    log_dir = str(tmp_path / "logs")
    run_probe(_probe(), _rc(), mock_policy=MockPolicy(rule="always"), seed=1, log_dir=log_dir)
    meta = _disk_meta(log_dir)
    # offline (no judge model) -> success is the binary verdict AND judge_applied is explicitly False
    assert meta["judge_applied"] is False
    assert meta["success"] is True  # always-vulnerable mock -> binary hit


def test_b1_disk_reread_warns_when_provisional_and_semantic(tmp_path, caplog):
    """A .eval re-read whose sample lacks judge_applied=True while the probe HAS a semantic check
    must warn loudly (provisional binary, not a final judge verdict) — never silently trusted."""
    from probe_engine.targets.mock import MockPolicy
    log_dir = str(tmp_path / "logs")
    run_probe(_probe(semantic=True), _rc(), mock_policy=MockPolicy(rule="always"), seed=1,
              log_dir=log_dir)
    logs = list_eval_logs(log_dir)
    log = read_eval_log(logs[0].name)
    # Simulate a PROVISIONAL on-disk sample (judge never persisted its marker).
    for s in log.samples:
        s.scores[next(iter(s.scores))].metadata.pop("judge_applied", None)
    with caplog.at_level(logging.WARNING):
        trials = eval_log_to_trials(log, has_semantic=True, probe_id="p1")
    assert trials  # still returns trials
    assert any("PROVISIONAL" in r.message for r in caplog.records)


def test_b1_no_warn_when_no_semantic_check(tmp_path, caplog):
    # offline binary-only probe: success==binary IS final, so no provisional warning ever fires.
    log = type("L", (), {"samples": [
        type("S", (), {"id": "v0", "epoch": 1, "scores": {"o": type(
            "Sc", (), {"metadata": {"success": True}})()}})()
    ]})()
    with caplog.at_level(logging.WARNING):
        eval_log_to_trials(log, has_semantic=False, probe_id="p1")
    assert not any("PROVISIONAL" in r.message for r in caplog.records)


# ---- B2: blind-spot skip, never crash --------------------------------------------------------

def _bridge_rc(judge_model=None) -> RunConfig:
    # bridge tier, NO tool_inventory -> state_changed/secret/authz oracles can never fire.
    return RunConfig(
        target=TargetConfig(tier="bridge", channels=["message", "history"], judge_model=judge_model),
        n_variants=1, epochs=1,
        thresholds=Thresholds(), run_id="r", timestamp="t",
    )


def _state_probe(pid: str) -> Probe:
    return Probe(
        id=pid, title="t", severity="high", intent="x",
        taxonomy_tags=[{"system": "atlas", "id": "AML.T0051.001"}],
        scenario=Scenario(type="chain", turns=[Turn(role="user", seed_prompts=["hi"])]),
        evaluation=Evaluation(binary=BinaryCheck(oracle="state_changed", args={})),
        provenance=Provenance(source="MCPTox"),
    )


async def _ext(request):
    return {"choices": [{"message": {"role": "assistant", "content": "ok", "tool_calls": []}}]}


def test_b2_blind_spot_skipped_and_surfaced(tmp_path, caplog):
    good = _probe("ok-probe")  # tool_called oracle is fine on bridge
    bad = _state_probe("state-probe")  # state_changed with no dangerous tool -> blind spot
    with caplog.at_level(logging.WARNING):
        evs = run_corpus([good, bad], _bridge_rc(), external=_ext,
                         log_dir=str(tmp_path / "logs"))
    ids = [e.probe_id for e in evs]
    assert "state-probe" not in ids          # the blind spot did NOT run / did NOT count as a pass
    assert "ok-probe" in ids                  # the adjudicable probe still ran
    assert evs.blind_spots == ["state-probe"]
    assert any("BLIND SPOT" in r.message for r in caplog.records)


def test_b2_blind_spot_emitted_via_progress(tmp_path):
    phases = []
    run_corpus([_state_probe("state-probe")], _bridge_rc(), external=_ext,
               log_dir=str(tmp_path / "logs"),
               progress=lambda phase, i, t, p, ev: phases.append((phase, p.id, ev)))
    assert ("skip", "state-probe", None) in phases


def test_b2_global_misconfig_still_crashes(tmp_path):
    # bridge with no external AND no endpoint is a GLOBAL config error, not a per-probe blind spot.
    import pytest
    rc = RunConfig(
        target=TargetConfig(tier="bridge", channels=["message"]),
        n_variants=1, epochs=1, thresholds=Thresholds(), run_id="r", timestamp="t",
    )
    with pytest.raises(ValueError):
        run_corpus([_probe("ok")], rc, log_dir=str(tmp_path / "logs"))


# ---- B3: resume / checkpoints ----------------------------------------------------------------

def test_b3_resume_reuses_matching_checkpoint(tmp_path, monkeypatch):
    from probe_engine.targets.mock import MockPolicy
    out = str(tmp_path / "out")
    # First run persists checkpoints.
    evs1 = run_corpus([_probe("a"), _probe("b")], _rc(), mock_policy=MockPolicy(rule="always"),
                      seed=1, resume=True, out_dir=out, log_dir=str(tmp_path / "logs"))
    assert (Path(out) / ".checkpoint" / "a.json").is_file()

    # Second run: make run_probe explode if called — a true resume must NOT call it.
    import probe_engine.run.executor as ex

    def _boom(*a, **k):
        raise AssertionError("run_probe must not be called for a checkpointed probe")

    monkeypatch.setattr(ex, "run_probe", _boom)
    evs2 = run_corpus([_probe("a"), _probe("b")], _rc(), mock_policy=MockPolicy(rule="always"),
                      seed=1, resume=True, out_dir=out, log_dir=str(tmp_path / "logs"))
    assert [e.probe_id for e in evs2] == ["a", "b"]
    assert [e.n_success for e in evs2] == [e.n_success for e in evs1]


def test_b3_config_hash_mismatch_reruns(tmp_path):
    from probe_engine.targets.mock import MockPolicy
    out = str(tmp_path / "out")
    run_corpus([_probe("a")], _rc(), mock_policy=MockPolicy(rule="always"),
               seed=1, resume=True, out_dir=out, log_dir=str(tmp_path / "logs"))
    # Corrupt the stored hash so it no longer matches -> the probe must re-run (no crash).
    cp = Path(out) / ".checkpoint" / "a.json"
    obj = json.loads(cp.read_text())
    obj["config_hash"] = "stale"
    cp.write_text(json.dumps(obj))
    evs = run_corpus([_probe("a")], _rc(), mock_policy=MockPolicy(rule="always"),
                     seed=1, resume=True, out_dir=out, log_dir=str(tmp_path / "logs"))
    assert [e.probe_id for e in evs] == ["a"]


def test_b3_resume_default_off_is_unchanged(tmp_path):
    from probe_engine.targets.mock import MockPolicy
    out = str(tmp_path / "out")
    run_corpus([_probe("a")], _rc(), mock_policy=MockPolicy(rule="always"),
               seed=1, log_dir=str(tmp_path / "logs"))  # resume defaults to False
    assert not (Path(out) / ".checkpoint").exists()


def test_b3_no_secret_in_checkpoint_or_hash(tmp_path):
    from probe_engine.targets.mock import MockPolicy
    out = str(tmp_path / "out")
    secret_prompt = "SYSTEM-PROMPT-SECRET-XYZ"
    secret_snippet = "PROTECTED-SNIPPET-SECRET-ABC"
    rc = _rc(system_prompt=secret_prompt, protected_snippets=[secret_snippet])
    run_corpus([_probe("a")], rc, mock_policy=MockPolicy(rule="always"),
               seed=1, resume=True, out_dir=out, log_dir=str(tmp_path / "logs"))
    blob = (Path(out) / ".checkpoint" / "a.json").read_text()
    assert secret_prompt not in blob
    assert secret_snippet not in blob
    # And the config hash itself must not move when only secrets change.
    h_with = checkpoint.config_hash(_probe("a"), rc, 2, 1, 1)
    h_without = checkpoint.config_hash(_probe("a"), _rc(), 2, 1, 1)
    assert h_with == h_without
