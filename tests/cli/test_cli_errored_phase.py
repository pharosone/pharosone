"""Regression for the D error-survival path at the CLI entry point.

(1) run_corpus emits progress("error", i, total, probe, None) for a probe whose every sample errored
on the target. cli._progress must handle that phase WITHOUT dereferencing the None evidence, and the
partial case (some probes still succeeded) stays exit-0 with the errored probe surfaced.
(2) When the WHOLE battery errors (zero usable observations), the run must exit NON-ZERO with a loud
banner — an all-errored run's overall_asr=0.00% must never read as a clean pass ("errors are never a
silent pass"). Fully offline: run_corpus is stubbed to drive the progress callback directly."""

from pathlib import Path

from typer.testing import CliRunner

import probe_engine.cli as cli_mod
from probe_engine.cli import app
from probe_engine.run.executor import EvidenceList
from probe_engine.run.executor import run_probe as _run_probe

ROOT = Path(__file__).parents[2]
runner = CliRunner()

CORPUS = str(ROOT / "corpus" / "probes")
FW = str(ROOT / "frameworks" / "aiuc-1.yaml")
CW = str(ROOT / "crosswalks" / "aiuc-1" / "crosswalk.yaml")

RUN_ARGS = [
    "run", "--corpus", CORPUS, "--framework", FW, "--crosswalk", CW,
    "--n-variants", "2", "--epochs", "1", "--mock-rule", "always",
]


def test_cli_handles_error_phase_without_crashing(tmp_path, monkeypatch):
    """One probe errors (ev=None), the rest succeed -> exit 0, ERRORED surfaced, no AttributeError."""

    def fake_run_corpus(probes, run_config, *, plan=None, progress=None,
                        resume=False, out_dir=None, adapter_channels=None, **kwargs):
        total = len(probes)
        errored = probes[0]
        if progress:
            progress("error", 1, total, errored, None)  # ev is None — must not be dereferenced
        results = EvidenceList()
        for i, p in enumerate(probes[1:], start=2):
            if progress:
                progress("start", i, total, p, None)
            ev = _run_probe(p, run_config, **kwargs)
            results.append(ev)
            if progress:
                progress("done", i, total, p, ev)
        results.errored = [errored.id]
        return results

    monkeypatch.setattr(cli_mod, "run_corpus", fake_run_corpus)
    out = tmp_path / "out"
    result = runner.invoke(app, RUN_ARGS + ["--out", str(out), "--log-dir", str(tmp_path / "logs")])
    assert result.exit_code == 0, result.stdout
    assert "ERRORED" in result.stdout        # errored probe surfaced
    assert "n_success" not in result.stdout  # the AttributeError message never appears
    assert (out / "report.json").exists()


def test_cli_all_errored_exits_nonzero(tmp_path, monkeypatch):
    """EVERY probe errors -> exit code 2 + a loud 'NOT a clean pass' banner; report still written."""

    def fake_run_corpus(probes, run_config, *, plan=None, progress=None,
                        resume=False, out_dir=None, adapter_channels=None, **kwargs):
        total = len(probes)
        results = EvidenceList()
        for i, p in enumerate(probes, start=1):
            if progress:
                progress("error", i, total, p, None)
        results.errored = [p.id for p in probes]
        return results

    monkeypatch.setattr(cli_mod, "run_corpus", fake_run_corpus)
    out = tmp_path / "out"
    result = runner.invoke(app, RUN_ARGS + ["--out", str(out), "--log-dir", str(tmp_path / "logs")])
    assert result.exit_code == 2, result.output
    assert "NOT a clean pass" in result.output   # loud banner distinguishes down-endpoint from robust
    assert (out / "report.json").exists()         # report is written BEFORE the non-zero exit
