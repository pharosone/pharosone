"""Regression for the B2 blind-spot skip phase at the CLI entry point.

run_corpus emits progress("skip", i, total, probe, None) for a probe whose oracle/channel the
target can't adjudicate (a blind spot). The in-repo CLI consumer (cli._progress) must handle that
phase explicitly and NOT dereference the None evidence (ev.n_success) — otherwise the very blind
spot B2 is meant to skip+surface crashes the whole CLI run (invariant 3: blind spots are surfaced,
never crash). Fully offline: run_corpus is stubbed to drive the progress callback directly."""

from pathlib import Path

from typer.testing import CliRunner

import probe_engine.cli as cli_mod
from probe_engine.cli import app
from probe_engine.run.executor import EvidenceList

ROOT = Path(__file__).parents[2]
runner = CliRunner()

CORPUS = str(ROOT / "corpus" / "probes")
FW = str(ROOT / "frameworks" / "aiuc-1.yaml")
CW = str(ROOT / "crosswalks" / "aiuc-1" / "crosswalk.yaml")


def test_cli_handles_skip_phase_without_crashing(tmp_path, monkeypatch):
    captured = {}

    def fake_run_corpus(probes, run_config, *, plan=None, progress=None,
                        resume=False, out_dir=None, adapter_channels=None, **kwargs):
        # Mirror the real run_corpus signature: resume/out_dir/adapter_channels are run-level params
        # the executor consumes itself, NOT forwarded to run_probe (forwarding would crash run_probe).
        # Drive the same phase sequence the real executor emits, including the new "skip" with ev=None
        # for a blind-spot probe. The old CLI _progress crashed here on ev.n_success.
        total = len(probes)
        skipped = probes[0]
        if progress:
            progress("skip", 1, total, skipped, None)  # ev is None — must not be dereferenced
        results = EvidenceList()
        for i, p in enumerate(probes[1:], start=2):
            if progress:
                progress("start", i, total, p, None)
            ev = _run_one(p, run_config, **kwargs)
            results.append(ev)
            if progress:
                progress("done", i, total, p, ev)
        results.blind_spots = [skipped.id]
        captured["blind_spots"] = results.blind_spots
        return results

    # reuse the real run_probe for the non-skipped probes so a valid report still builds
    from probe_engine.run.executor import run_probe as _run_probe

    def _run_one(p, rc, **kwargs):
        return _run_probe(p, rc, **kwargs)

    monkeypatch.setattr(cli_mod, "run_corpus", fake_run_corpus)

    out = tmp_path / "out"
    result = runner.invoke(app, [
        "run", "--corpus", CORPUS, "--framework", FW, "--crosswalk", CW,
        "--out", str(out), "--n-variants", "2", "--epochs", "1",
        "--mock-rule", "always", "--log-dir", str(tmp_path / "logs"),
    ])
    assert result.exit_code == 0, result.stdout
    assert "SKIPPED" in result.stdout  # blind spot surfaced, not crashed
    assert "n_success" not in result.stdout  # the AttributeError message never appears
    assert (out / "report.json").exists()
    assert captured["blind_spots"]  # the skipped probe id was surfaced
