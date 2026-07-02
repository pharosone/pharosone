"""Unit tests for the false-positive-prone binary-oracle guard predicate (scoring/unverified) and the
CLI's loud warning when the selected corpus contains such probes with no judge configured."""

from pathlib import Path

from typer.testing import CliRunner

from probe_engine.cli import app
from probe_engine.scoring.unverified import (
    FP_PRONE_ORACLES,
    is_fp_prone_oracle,
    unverified_reason,
)

ROOT = Path(__file__).parents[2]
CORPUS = str(ROOT / "corpus" / "probes")
FW = str(ROOT / "frameworks" / "aiuc-1.yaml")
CW = str(ROOT / "crosswalks" / "aiuc-1" / "crosswalk.yaml")
runner = CliRunner()


# ---- predicate ----------------------------------------------------------------------------------

def test_fp_prone_set_is_prompt_leak_and_contains():
    assert FP_PRONE_ORACLES == frozenset({"prompt_leak", "contains"})
    assert is_fp_prone_oracle("prompt_leak") and is_fp_prone_oracle("contains")
    assert not is_fp_prone_oracle("tool_called")
    assert not is_fp_prone_oracle("output_pattern")
    assert not is_fp_prone_oracle(None)


def test_unjudged_fp_prone_oracle_is_downgraded():
    assert unverified_reason(
        oracle_kind="contains", judge_applied=False, has_protected_snippets=False
    )
    r = unverified_reason(
        oracle_kind="prompt_leak", judge_applied=False, has_protected_snippets=True
    )
    assert r  # downgraded even WITH snippets when unjudged (refusal-quoting FP)


def test_empty_protected_snippets_called_out_for_prompt_leak():
    r = unverified_reason(
        oracle_kind="prompt_leak", judge_applied=False, has_protected_snippets=False
    )
    assert r is not None and "protected_snippets" in r


def test_judged_verdict_is_never_downgraded():
    assert unverified_reason(
        oracle_kind="prompt_leak", judge_applied=True, has_protected_snippets=False
    ) is None
    assert unverified_reason(
        oracle_kind="contains", judge_applied=True, has_protected_snippets=True
    ) is None


def test_non_fp_prone_oracle_is_never_downgraded():
    for kind in ("tool_called", "output_pattern", "state_changed", "code_pattern", None):
        assert unverified_reason(
            oracle_kind=kind, judge_applied=False, has_protected_snippets=False
        ) is None


# ---- CLI warning (validate path) ----------------------------------------------------------------

def test_cli_warns_when_fp_prone_probes_run_without_judge(tmp_path):
    """A default run (no judge_model) over the corpus selects prompt_leak/contains probes -> a loud
    WARNING that they will be UNVERIFIED, plus the empty-protected_snippets call-out for prompt_leak."""
    result = runner.invoke(app, [
        "run", "--corpus", CORPUS, "--framework", FW, "--crosswalk", CW,
        "--out", str(tmp_path / "out"), "--n-variants", "2", "--epochs", "1",
        "--mock-rule", "refuse", "--log-dir", str(tmp_path / "logs"),
    ])
    assert result.exit_code == 0, result.stdout
    assert "UNVERIFIED" in result.stdout
    assert "no judge_model is configured" in result.stdout
    # the prompt_leak / empty-snippets call-out is present too
    assert "protected_snippets" in result.stdout
