"""Provenance metadata (Task 1): ADDITIVE run provenance is stamped into the .eval log's metadata
without ever overwriting Inspect's core `eval-model` field (which genuinely ran `mockllm/model`
for the mock/bridge tiers). All offline, mock tier — no network, no API key, no judge model.

Covered:
  * `_run_provenance` is a pure enrichment of the caller dict: None -> None (byte-identical), else
    engine adds `tier` (+ the mockllm placeholder note for mock/bridge) and the caller's keys WIN.
  * A real mock-tier `run_corpus`/`run_probe` with `run_metadata` writes those keys into
    `read_eval_log(...).eval.metadata`, leaves `eval.model == "mockllm/model"`, and does NOT change
    the scores/verdict/sample count vs the identical run WITHOUT metadata.
"""

from inspect_ai.log import list_eval_logs, read_eval_log

from probe_engine.domain.probe import (
    BinaryCheck,
    Evaluation,
    Probe,
    Provenance,
    Scenario,
    Turn,
)
from probe_engine.domain.run import RunConfig, TargetConfig, Thresholds
from probe_engine.run.executor import (
    _EVAL_MODEL_PLACEHOLDER_NOTE,
    _run_provenance,
    run_corpus,
    run_probe,
)
from probe_engine.targets.mock import MockPolicy


def _probe(pid: str = "p1") -> Probe:
    return Probe(
        id=pid, title="t", severity="high", intent="x",
        taxonomy_tags=[{"system": "atlas", "id": "AML.T0051.001"}],
        scenario=Scenario(type="chain", turns=[Turn(role="user", seed_prompts=["hi"])]),
        evaluation=Evaluation(binary=BinaryCheck(oracle="tool_called", args={"tool": "move_file"})),
        provenance=Provenance(source="MCPTox"),
    )


def _rc(tier: str = "mock", **target_kwargs) -> RunConfig:
    return RunConfig(
        target=TargetConfig(tier=tier, **target_kwargs),
        n_variants=3, epochs=2,
        thresholds=Thresholds(), run_id="r", timestamp="2026-06-30T00:00:00Z",
    )


def _eval_metadata_on_disk(log_dir: str) -> dict | None:
    logs = list_eval_logs(log_dir)
    assert len(logs) == 1, f"expected exactly one .eval, got {len(logs)}"
    return read_eval_log(logs[0].name).eval.metadata


def _eval_log_on_disk(log_dir: str):
    logs = list_eval_logs(log_dir)
    assert len(logs) == 1
    return read_eval_log(logs[0].name)


# ---- _run_provenance: pure enrichment logic ---------------------------------------------------

def test_run_provenance_none_is_byte_identical():
    # No stamping requested -> None threaded into eval == today's exact call.
    assert _run_provenance(_rc(), None) is None


def test_run_provenance_mock_adds_tier_and_placeholder_note():
    prov = _run_provenance(_rc("mock"), {})
    assert prov == {
        "tier": "mock",
        "eval_model_placeholder_note": _EVAL_MODEL_PLACEHOLDER_NOTE["mock"],
    }


def test_run_provenance_bridge_adds_tier_and_placeholder_note():
    prov = _run_provenance(_rc("bridge", channels=["message"]), {})
    assert prov == {
        "tier": "bridge",
        "eval_model_placeholder_note": _EVAL_MODEL_PLACEHOLDER_NOTE["bridge"],
    }


def test_run_provenance_model_tier_has_no_placeholder_note():
    # The `model` tier's eval-model IS the real model, so there is no placeholder artifact to explain.
    prov = _run_provenance(_rc("model", provider="openrouter", model="anthropic/claude-x"), {})
    assert prov == {"tier": "model"}
    assert "eval_model_placeholder_note" not in prov


def test_run_provenance_merges_caller_keys():
    prov = _run_provenance(
        _rc("bridge", channels=["message"]),
        {"sut_model": "deepseek/deepseek-v4-flash", "gen_model": "z-ai/glm-5.2",
         "judge_model": "z-ai/glm-5.2", "variant": "hardened"},
    )
    assert prov == {
        "tier": "bridge",
        "eval_model_placeholder_note": _EVAL_MODEL_PLACEHOLDER_NOTE["bridge"],
        "sut_model": "deepseek/deepseek-v4-flash",
        "gen_model": "z-ai/glm-5.2",
        "judge_model": "z-ai/glm-5.2",
        "variant": "hardened",
    }


def test_run_provenance_caller_wins_on_collision():
    # The caller can override even the engine-supplied keys (it knows the truth for the bridge tier).
    prov = _run_provenance(_rc("bridge", channels=["message"]),
                           {"tier": "bridge-inprocess", "eval_model_placeholder_note": "custom"})
    assert prov["tier"] == "bridge-inprocess"
    assert prov["eval_model_placeholder_note"] == "custom"


# ---- integration: the keys reach the written .eval, and the core field is intact --------------

def test_run_metadata_lands_in_eval_log_metadata(tmp_path):
    log_dir = str(tmp_path / "logs")
    meta_in = {"sut_model": "deepseek/deepseek-v4-flash", "gen_model": "z-ai/glm-5.2",
               "judge_model": "z-ai/glm-5.2", "variant": "default"}
    run_probe(_probe(), _rc(), mock_policy=MockPolicy(rule="always"), seed=1,
              log_dir=log_dir, run_metadata=meta_in)

    log = _eval_log_on_disk(log_dir)
    # The provenance is present, exactly (engine adds tier + note; caller keys carried through).
    assert log.eval.metadata == {
        "tier": "mock",
        "eval_model_placeholder_note": _EVAL_MODEL_PLACEHOLDER_NOTE["mock"],
        "sut_model": "deepseek/deepseek-v4-flash",
        "gen_model": "z-ai/glm-5.2",
        "judge_model": "z-ai/glm-5.2",
        "variant": "default",
    }
    # CRITICAL: the core eval-model field is untouched — Inspect genuinely ran the placeholder.
    assert log.eval.model == "mockllm/model"


def test_run_metadata_does_not_change_scores_or_samples(tmp_path):
    # Same probe/policy/seed, run WITH and WITHOUT provenance: verdict + trials + per-sample scores
    # are byte-for-byte the same (provenance is purely additive metadata).
    log_with = str(tmp_path / "with")
    log_without = str(tmp_path / "without")
    ev_with = run_probe(_probe(), _rc(), mock_policy=MockPolicy(rule="always"), seed=7,
                        log_dir=log_with, run_metadata={"variant": "default"})
    ev_without = run_probe(_probe(), _rc(), mock_policy=MockPolicy(rule="always"), seed=7,
                           log_dir=log_without)

    assert (ev_with.n_success, ev_with.n_trials, ev_with.asr, ev_with.status) == \
           (ev_without.n_success, ev_without.n_trials, ev_without.asr, ev_without.status)
    assert ev_with.n_trials == 6  # 3 variants x 2 epochs

    log_w = _eval_log_on_disk(log_with)
    log_wo = _eval_log_on_disk(log_without)
    assert len(log_w.samples) == len(log_wo.samples) == 6
    # Per-sample score value + success/judge_applied markers identical across the two runs.
    def _score_tuples(log):
        out = []
        for s in sorted(log.samples, key=lambda s: (str(s.id), s.epoch)):
            sc = s.scores[next(iter(s.scores))]
            out.append((str(s.id), s.epoch, sc.value,
                        sc.metadata.get("success"), sc.metadata.get("judge_applied")))
        return out
    assert _score_tuples(log_w) == _score_tuples(log_wo)


def test_no_run_metadata_writes_no_eval_metadata(tmp_path):
    # Byte-identical prior behavior: without run_metadata the eval log carries no metadata.
    log_dir = str(tmp_path / "logs")
    run_probe(_probe(), _rc(), mock_policy=MockPolicy(rule="always"), seed=1, log_dir=log_dir)
    assert _eval_metadata_on_disk(log_dir) is None


def test_run_corpus_threads_run_metadata_to_every_probe(tmp_path):
    log_dir = str(tmp_path / "logs")
    run_corpus([_probe("a"), _probe("b")], _rc(), mock_policy=MockPolicy(rule="always"),
               seed=1, log_dir=log_dir, run_metadata={"variant": "hardened", "sut_model": "x/y"})
    logs = list_eval_logs(log_dir)
    assert len(logs) == 2  # one .eval per probe
    for entry in logs:
        meta = read_eval_log(entry.name).eval.metadata
        assert meta["tier"] == "mock"
        assert meta["variant"] == "hardened"
        assert meta["sut_model"] == "x/y"
        assert meta["eval_model_placeholder_note"] == _EVAL_MODEL_PLACEHOLDER_NOTE["mock"]
