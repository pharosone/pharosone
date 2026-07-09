import json
from pathlib import Path

from typer.testing import CliRunner

from probe_engine.cli import app

ROOT = Path(__file__).parents[2]
EX = ROOT / "harness" / "example-agent"
runner = CliRunner()


def test_validate_artifacts_passport_ok():
    result = runner.invoke(app, ["validate-artifacts", "passport", str(EX / "PASSPORT.md")])
    assert result.exit_code == 0, result.stdout
    assert "OK" in result.stdout


def test_validate_artifacts_seams_ok():
    result = runner.invoke(app, ["validate-artifacts", "seams", str(EX / "SEAMS.md")])
    assert result.exit_code == 0, result.stdout
    assert "OK" in result.stdout


def test_validate_artifacts_invalid_exits_one(tmp_path):
    bad = tmp_path / "passport.json"
    bad.write_text(json.dumps({"topology": "serverless"}), encoding="utf-8")
    result = runner.invoke(app, ["validate-artifacts", "passport", str(bad)])
    assert result.exit_code == 1


def test_validate_artifacts_missing_file_exits_one(tmp_path):
    result = runner.invoke(app, ["validate-artifacts", "seams", str(tmp_path / "nope.json")])
    assert result.exit_code == 1
