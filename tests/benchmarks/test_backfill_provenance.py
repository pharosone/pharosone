"""Task 2 — backfill_provenance.py: attribution, idempotency, and additive-only round-trip safety.

Pure helpers are unit-tested for exact behavior; a real offline mock `.eval` exercises the full
loop end-to-end: dry-run plans a patch, `--apply` writes ADDITIVE metadata WITHOUT changing the core
eval-model / scores / samples, a re-run is idempotent (skip), out-of-window logs are untouched, and a
non-placeholder eval-model is skipped by the safety guard.
"""

from argparse import Namespace
from pathlib import Path

from inspect_ai import Task
from inspect_ai import eval as inspect_eval
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.log import read_eval_log, write_eval_log
from inspect_ai.scorer import Score, accuracy, scorer

from probe_engine.run.executor import _EVAL_MODEL_PLACEHOLDER_NOTE

from benchmarks.oracle_vs_judge.backfill_provenance import (
    build_target,
    log_is_judged,
    plan_metadata,
    run,
    score_signature,
    variant_for,
)


# ---- pure helpers ----------------------------------------------------------------------------

def test_variant_for_split_boundary():
    # < split -> before; == split and > split -> after (at/after the cutoff is the second run).
    assert variant_for(99.0, 100.0, "default", "hardened") == "default"
    assert variant_for(100.0, 100.0, "default", "hardened") == "hardened"
    assert variant_for(101.0, 100.0, "default", "hardened") == "hardened"


def test_build_target_bridge_judged_omits_gen_by_default():
    t = build_target(tier="bridge", variant="default", sut_model="deepseek/deepseek-v4-flash",
                     judge_model="z-ai/glm-5.2", gen_model=None)
    assert t["tier"] == "bridge"
    assert t["sut_model"] == "deepseek/deepseek-v4-flash"
    assert t["variant"] == "default"
    assert t["judge_model"] == "z-ai/glm-5.2"
    assert t["eval_model_placeholder_note"] == _EVAL_MODEL_PLACEHOLDER_NOTE["bridge"]
    assert t["provenance_backfilled"] is True
    assert "gen_model" not in t  # arm-specific -> omitted unless explicitly opted in


def test_build_target_unjudged_omits_judge_model():
    t = build_target(tier="bridge", variant="hardened", sut_model="x/y",
                     judge_model=None, gen_model=None)
    assert "judge_model" not in t
    assert t["variant"] == "hardened"


def test_build_target_stamp_gen_when_provided():
    t = build_target(tier="bridge", variant="default", sut_model="x/y",
                     judge_model="j", gen_model="z-ai/glm-5.2")
    assert t["gen_model"] == "z-ai/glm-5.2"


def test_plan_metadata_none_existing_patches():
    target = {"tier": "bridge", "sut_model": "s", "variant": "default"}
    action, meta = plan_metadata(None, target)
    assert action == "patch"
    assert meta == target
    assert meta is not target  # a copy, not the same object


def test_plan_metadata_identical_skips():
    target = {"tier": "bridge", "sut_model": "s", "variant": "default"}
    action, meta = plan_metadata(dict(target), target)
    assert action == "skip-identical"


def test_plan_metadata_core_present_but_no_markers_is_skip_present():
    # A forward-stamped log already carries the CORE keys (no backfill markers) -> leave it alone.
    existing = {"tier": "bridge", "sut_model": "s", "variant": "default", "gen_model": "g"}
    target = {"tier": "bridge", "sut_model": "s", "variant": "default",
              "provenance_backfilled": True, "provenance_source": "x"}
    action, _ = plan_metadata(existing, target)
    assert action == "skip-present"


def test_plan_metadata_conflict_never_clobbers():
    existing = {"tier": "bridge", "sut_model": "OTHER", "variant": "default"}
    target = {"tier": "bridge", "sut_model": "deepseek/deepseek-v4-flash", "variant": "default"}
    action, meta = plan_metadata(existing, target)
    assert action == "conflict"
    assert meta is None


def test_plan_metadata_merges_unrelated_keys():
    existing = {"foo": "bar"}
    target = {"tier": "bridge", "sut_model": "s", "variant": "default"}
    action, meta = plan_metadata(existing, target)
    assert action == "patch"
    assert meta == {"foo": "bar", "tier": "bridge", "sut_model": "s", "variant": "default"}


def _fake_log(judge_applied_values: list):
    samples = []
    for i, ja in enumerate(judge_applied_values):
        score = type("Sc", (), {"value": 1.0, "metadata": {"success": True, "judge_applied": ja}})()
        samples.append(type("S", (), {"id": f"v{i}", "epoch": 1, "scores": {"o": score}})())
    return type("L", (), {"samples": samples})()


def test_log_is_judged():
    assert log_is_judged(_fake_log([True, False])) is True
    assert log_is_judged(_fake_log([False, False])) is False
    assert log_is_judged(_fake_log([])) is False


def test_score_signature_detects_score_change():
    a = _fake_log([True, True])
    b = _fake_log([True, True])
    assert score_signature(a) == score_signature(b)
    b.samples[0].scores["o"].value = 0.0
    assert score_signature(a) != score_signature(b)


# ---- integration: real offline mock .eval round-trip -----------------------------------------

@scorer(metrics=[accuracy()])
def _dummy_scorer():
    async def score(state, target):
        return Score(value=1.0, metadata={"success": True, "binary_hit": True, "judge_applied": True})
    return score


def _make_eval(log_dir: str, sample_id: str = "v1") -> Path:
    task = Task(dataset=MemoryDataset([Sample(input="hi", target="robust", id=sample_id)]),
                scorer=_dummy_scorer())
    inspect_eval(task, model="mockllm/model", display="none", log_dir=log_dir)
    evals = sorted(Path(log_dir).glob("*.eval"))
    assert len(evals) == 1
    return evals[0]


def _args(logs, **over) -> Namespace:
    base = dict(
        logs=logs, exclude=[], since="2026-06-30T12:37:00", until="2026-06-30T20:35:00",
        variant_split="2026-06-30T16:38:00", variant_before="default", variant_after="hardened",
        sut_model="deepseek/deepseek-v4-flash", gen_model="z-ai/glm-5.2",
        judge_model="z-ai/glm-5.2", tier="bridge", require_eval_model="mockllm/model",
        stamp_gen_model=False, max_logs=0, progress_every=50, max_failures=10, apply=False,
    )
    base.update(over)
    return Namespace(**base)


def _set_mtime(path: Path, iso: str) -> None:
    from datetime import datetime
    t = datetime.fromisoformat(iso).timestamp()
    import os
    os.utime(path, (t, t))


def test_apply_is_additive_idempotent_and_verified(tmp_path):
    log_dir = str(tmp_path / "logs")
    ev = _make_eval(log_dir)
    _set_mtime(ev, "2026-06-30T14:00:00")  # inside window, before split -> "default"
    globs = [str(tmp_path / "logs" / "*.eval")]

    before = score_signature(read_eval_log(str(ev)))

    # 1. dry run -> plans exactly one patch, writes nothing.
    dry = run(_args(globs, apply=False))
    assert dry["dry_run"] is True
    assert dry["counts"]["would_patch"] == 1
    assert read_eval_log(str(ev)).eval.metadata is None  # nothing written

    # 2. apply -> additive metadata present, core eval-model + scores/samples unchanged.
    res = run(_args(globs, apply=True))
    assert res["applied"] == 1
    log = read_eval_log(str(ev))
    md = log.eval.metadata
    assert md["tier"] == "bridge"
    assert md["sut_model"] == "deepseek/deepseek-v4-flash"
    assert md["variant"] == "default"
    assert md["judge_model"] == "z-ai/glm-5.2"
    assert md["eval_model_placeholder_note"] == _EVAL_MODEL_PLACEHOLDER_NOTE["bridge"]
    assert md["provenance_backfilled"] is True
    assert "gen_model" not in md  # arm-specific -> omitted
    assert log.eval.model == "mockllm/model"        # core field untouched
    assert score_signature(log) == before           # scores/samples byte-stable

    # 3. re-run apply -> idempotent (already provenance'd, nothing patched).
    res2 = run(_args(globs, apply=True))
    assert res2["applied"] == 0
    assert res2["counts"]["skip_already_provenanced"] == 1


def test_out_of_window_log_untouched(tmp_path):
    log_dir = str(tmp_path / "logs")
    ev = _make_eval(log_dir)
    _set_mtime(ev, "2026-06-29T10:00:00")  # OUTSIDE the study window
    globs = [str(tmp_path / "logs" / "*.eval")]
    res = run(_args(globs, apply=True))
    assert res["counts"].get("in_window", 0) == 0
    assert read_eval_log(str(ev)).eval.metadata is None  # never touched


def test_unjudged_log_gets_no_judge_model(tmp_path):
    # A log with no real judge verdict (judge_applied not True) must be stamped WITHOUT judge_model.
    @scorer(metrics=[accuracy()])
    def _nojudge():
        async def score(state, target):
            return Score(value=0.0, metadata={"success": False, "judge_applied": False})
        return score

    log_dir = str(tmp_path / "logs")
    task = Task(dataset=MemoryDataset([Sample(input="hi", target="robust", id="v1")]),
                scorer=_nojudge())
    inspect_eval(task, model="mockllm/model", display="none", log_dir=log_dir)
    ev = sorted(Path(log_dir).glob("*.eval"))[0]
    _set_mtime(ev, "2026-06-30T18:00:00")  # after split -> "hardened"
    globs = [str(tmp_path / "logs" / "*.eval")]

    res = run(_args(globs, apply=True))
    assert res["applied"] == 1
    md = read_eval_log(str(ev)).eval.metadata
    assert md["variant"] == "hardened"
    assert "judge_model" not in md
    assert md["sut_model"] == "deepseek/deepseek-v4-flash"


def test_non_placeholder_eval_model_skipped(tmp_path):
    # A real eval-model in the window is a different run -> the safety guard skips it (never patched).
    log_dir = str(tmp_path / "logs")
    ev = _make_eval(log_dir)
    # Rewrite eval-model to a real model to simulate a genuine `model`-tier log, THEN set the mtime
    # (write_eval_log rewrites the file and resets its mtime to "now").
    log = read_eval_log(str(ev))
    log.eval.model = "openrouter/deepseek/deepseek-v4-flash"
    write_eval_log(log, str(ev))
    _set_mtime(ev, "2026-06-30T14:00:00")
    globs = [str(tmp_path / "logs" / "*.eval")]

    res = run(_args(globs, apply=True))
    assert res["counts"]["non_placeholder"] == 1
    assert res["counts"].get("would_patch", 0) == 0
    assert read_eval_log(str(ev)).eval.metadata is None  # untouched
