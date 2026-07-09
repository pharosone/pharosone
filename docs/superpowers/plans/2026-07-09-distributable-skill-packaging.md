# Distributable Skill Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the pharosone onboarding suite install correctly across Claude Code / Cursor / other agents in any project (npx skills + native Cursor plugin), fully decoupled from hardcoded paths, with the `probe-engine` console script as the sole runtime interface.

**Architecture:** Move the stdlib artifact validator + JSON schemas from inside the `pharosone` skill into the `probe_engine` package and expose it as `probe-engine validate-artifacts`. Relocate the 6 skills to a canonical top-level `skills/` (with `.claude/skills` a symlink), add `.cursor-plugin/` manifests, make every intra-skill path self-relative, and harden `.gitignore` so untracked customer recon can never enter an sdist.

**Tech Stack:** Python 3.12+, Typer/Click CLI, hatchling build backend, pytest + typer.testing.CliRunner, stdlib-only validator (no new deps).

## Global Constraints

- Package name `pharosone-security-scanner`; this refactor targets version **0.1.1** (0.1.0 already on PyPI, immutable).
- GitHub repo `pharosone/pharosone`; canonical skills location becomes top-level `skills/`.
- Validator stays **stdlib-only** — no `jsonschema` or other new dependency.
- All tests run **offline** (no network / API keys / Docker); mock tier only.
- Work on branch `feat/distributable-skill-packaging`. TDD, frequent commits.
- Do **not** publish to PyPI in this plan — publishing 0.1.1 is a separate, explicit-go step.

---

### Task 1: Move the validator + schemas into `probe_engine.onboarding` (behaviour parity)

**Files:**
- Create: `src/probe_engine/onboarding/__init__.py`
- Create: `src/probe_engine/onboarding/validate.py`
- Create: `src/probe_engine/onboarding/schemas/passport.schema.json` (copy of `.claude/skills/pharosone/schemas/passport.schema.json`)
- Create: `src/probe_engine/onboarding/schemas/seams.schema.json` (copy of `.claude/skills/pharosone/schemas/seams.schema.json`)
- Modify: `tests/skills/test_validate_artifacts.py` (lines 23–31 module loader; line 438 subprocess argv)

**Interfaces:**
- Produces: `probe_engine.onboarding.validate` module exposing `validate(kind: str, instance) -> list[str]`, `validate_passport(instance) -> list[str]`, `validate_seams(instance) -> list[str]`, `load_artifact(path: Path) -> Any`, `load_schema(kind: str) -> dict`, `ArtifactError`, and `main(argv: list[str] | None = None) -> int`. Runnable as `python -m probe_engine.onboarding.validate <passport|seams> <path>`.

- [ ] **Step 1: Copy the schemas into the package**

```bash
mkdir -p src/probe_engine/onboarding/schemas
cp .claude/skills/pharosone/schemas/passport.schema.json src/probe_engine/onboarding/schemas/
cp .claude/skills/pharosone/schemas/seams.schema.json    src/probe_engine/onboarding/schemas/
: > src/probe_engine/onboarding/__init__.py
```

- [ ] **Step 2: Repoint the test's module loader to the package (make it fail first)**

Replace lines 21–31 of `tests/skills/test_validate_artifacts.py` (the `REPO_ROOT`/`VALIDATOR_PATH`/`importlib` block) with:

```python
REPO_ROOT = Path(__file__).resolve().parents[2]

import probe_engine.onboarding.validate as validator
```

And change `_run_cli` (line ~436) to invoke the module instead of the old script path:

```python
def _run_cli(kind: str, path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "probe_engine.onboarding.validate", kind, str(path)],
        capture_output=True,
        text=True,
    )
```

- [ ] **Step 3: Run the test — verify it fails (module does not exist yet)**

Run: `uv run pytest tests/skills/test_validate_artifacts.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'probe_engine.onboarding.validate'`

- [ ] **Step 4: Create `validate.py` — faithful port, schemas via importlib.resources**

Copy `.claude/skills/pharosone/scripts/validate_artifacts.py` verbatim into `src/probe_engine/onboarding/validate.py`, then make exactly two changes:

1. Replace the schema-location lines (the old `SCHEMA_DIR = Path(__file__)...` + `load_schema`) with:

```python
from importlib import resources

_SCHEMA_FILES = {"passport": "passport.schema.json", "seams": "seams.schema.json"}


def load_schema(kind: str) -> dict[str, Any]:
    """Read and parse a packaged schema file by artifact kind."""
    resource = resources.files("probe_engine.onboarding.schemas").joinpath(_SCHEMA_FILES[kind])
    return json.loads(resource.read_text(encoding="utf-8"))
```

2. Delete the now-unused `from pathlib import Path` only if `Path` is otherwise unused — it is still used by `load_artifact`/`main`, so **keep it**. Keep `main()` and the `if __name__ == "__main__": raise SystemExit(main())` block unchanged so `python -m probe_engine.onboarding.validate` works.

Everything else (`load_artifact`, `_validate_*`, `_semantic_*`, `validate_passport`, `validate_seams`, `validate`, `ArtifactError`) is copied unchanged.

- [ ] **Step 5: Run the full validator test file — verify it passes**

Run: `uv run pytest tests/skills/test_validate_artifacts.py -q`
Expected: PASS (all ~50 cases, including the 3 subprocess CLI cases).

- [ ] **Step 6: Commit**

```bash
git add src/probe_engine/onboarding tests/skills/test_validate_artifacts.py
git commit -m "refactor: move onboarding artifact validator into probe_engine package"
```

---

### Task 2: Add the `probe-engine validate-artifacts` CLI command

**Files:**
- Modify: `src/probe_engine/cli.py` (add an `ArtifactKind` enum + a new `@app.command`)
- Create: `tests/cli/test_cli_validate_artifacts.py`

**Interfaces:**
- Consumes: `probe_engine.onboarding.validate.load_artifact`, `.validate`, `.ArtifactError` (Task 1).
- Produces: CLI command `validate-artifacts <passport|seams> <path>` — exit 0 + `OK: …` on stdout when valid; exit 1 + `INVALID: …` / `error: file not found: …` on stderr otherwise.

- [ ] **Step 1: Write the failing CLI tests**

Create `tests/cli/test_cli_validate_artifacts.py`:

```python
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
```

- [ ] **Step 2: Run — verify it fails (no such command)**

Run: `uv run pytest tests/cli/test_cli_validate_artifacts.py -q`
Expected: FAIL — non-zero exit / "No such command 'validate-artifacts'".

- [ ] **Step 3: Add the command to `cli.py`**

Add near the top of `src/probe_engine/cli.py` (after the existing imports):

```python
from enum import Enum


class ArtifactKind(str, Enum):
    passport = "passport"
    seams = "seams"
```

Add this command (anywhere among the other `@app.command()` functions):

```python
@app.command("validate-artifacts")
def validate_artifacts(
    kind: ArtifactKind = typer.Argument(..., help="artifact kind: passport | seams"),
    path: str = typer.Argument(..., help="path to a .json artifact or a .md file with a ```json block"),
) -> None:
    """Validate a pharosone onboarding artifact (passport/seams) against its schema + invariants."""
    from probe_engine.onboarding.validate import ArtifactError, load_artifact, validate

    p = Path(path)
    if not p.exists():
        typer.echo(f"error: file not found: {p}", err=True)
        raise typer.Exit(1)
    try:
        instance = load_artifact(p)
    except ArtifactError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)
    errors = validate(kind.value, instance)
    if errors:
        typer.echo(f"INVALID: {p} ({kind.value}) — {len(errors)} problem(s):", err=True)
        for error in errors:
            typer.echo(f"  - {error}", err=True)
        raise typer.Exit(1)
    typer.echo(f"OK: {p} ({kind.value}) is valid.")
```

- [ ] **Step 4: Run — verify pass**

Run: `uv run pytest tests/cli/test_cli_validate_artifacts.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/probe_engine/cli.py tests/cli/test_cli_validate_artifacts.py
git commit -m "feat(cli): add 'probe-engine validate-artifacts' command"
```

---

### Task 3: Ensure the schemas ship in the built wheel

**Files:**
- Modify: `pyproject.toml` (`[tool.hatch.build.targets.wheel]`)

**Interfaces:**
- Produces: wheel that contains `probe_engine/onboarding/schemas/passport.schema.json` and `seams.schema.json`, so `importlib.resources` resolves them post-install.

- [ ] **Step 1: Force-include the schema data in the wheel**

In `pyproject.toml`, extend the existing wheel target:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/probe_engine"]
artifacts = ["src/probe_engine/onboarding/schemas/*.json"]
```

- [ ] **Step 2: Build and assert the schemas are inside the wheel**

Run:
```bash
rm -rf dist && uv build --wheel >/dev/null 2>&1
unzip -l dist/pharosone_security_scanner-*.whl | grep -c "probe_engine/onboarding/schemas/.*\.json"
```
Expected: `2`

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "build: ship onboarding JSON schemas as wheel package data"
```

---

### Task 4: Retire the skill-embedded validator; point skill docs at the CLI

**Files:**
- Delete: `.claude/skills/pharosone/scripts/` (the whole dir, incl. `validate_artifacts.py` + `__pycache__`)
- Delete: `.claude/skills/pharosone/schemas/` (the whole dir — now shipped in the package)
- Modify: `.claude/skills/validate-and-certify/SKILL.md` (lines 44–45, 48–49)
- Modify: `.claude/skills/pharosone/PIPELINE_DESIGN.md` (lines 33–34, and the "single source of truth = the JSON schemas" prose)

- [ ] **Step 1: Delete the now-duplicated skill assets**

```bash
git rm -r .claude/skills/pharosone/scripts
git rm -r .claude/skills/pharosone/schemas
```

- [ ] **Step 2: Rewrite the validator invocations in `validate-and-certify/SKILL.md`**

Replace the two `python .claude/skills/pharosone/scripts/validate_artifacts.py …` lines with:

```
probe-engine validate-artifacts passport harness/<agent>/PASSPORT.md
probe-engine validate-artifacts seams    harness/<agent>/SEAMS.md
```

And replace the "Schemas: `.claude/skills/pharosone/schemas/…`" sentence with:

```
The passport/seams JSON schemas ship inside the probe-engine package
(`probe_engine/onboarding/schemas/`); `probe-engine validate-artifacts` enforces:
```

- [ ] **Step 3: Rewrite the same references in `pharosone/PIPELINE_DESIGN.md`**

Replace the `python .claude/skills/pharosone/scripts/validate_artifacts.py …` block (lines 33–34) with the same two `probe-engine validate-artifacts …` lines. In the surrounding prose, change "the validator (stdlib only)" / schema-location wording to say the validator + schemas now live in `probe_engine.onboarding` and are invoked via `probe-engine validate-artifacts`.

- [ ] **Step 4: Verify no stale references remain and tests still pass**

Run:
```bash
grep -rn "validate_artifacts.py\|pharosone/schemas\|skills/pharosone/scripts" .claude/skills && echo "STALE REF FOUND" || echo "clean"
uv run pytest tests/skills/test_validate_artifacts.py tests/cli/test_cli_validate_artifacts.py -q
```
Expected: `clean`, then PASS.

- [ ] **Step 5: Commit**

```bash
git add -A .claude/skills
git commit -m "refactor(skills): call 'probe-engine validate-artifacts' instead of the embedded script"
```

---

### Task 5: Make every intra-skill absolute path self-relative

**Files:**
- Modify: `.claude/skills/find-agent-seams/SKILL.md` (line ~41)
- Modify: `.claude/skills/validate-and-certify/SKILL.md` (line ~150 — the `templates/wishlist_template.md` ref)
- Audit + modify any other `.claude/skills/...` path inside a skill body (e.g. `build-run-profile`, `generate-agent-shim` template refs)

- [ ] **Step 1: List every remaining absolute skill path**

Run: `grep -rn "\.claude/skills" .claude/skills`
Note each hit; each must become relative to the skill that owns it (e.g. `references/waist-detectors.md`, `../pharosone/templates/wishlist_template.md`).

- [ ] **Step 2: Rewrite each hit to a self-relative path**

For `find-agent-seams/SKILL.md`: `\`.claude/skills/find-agent-seams/references/waist-detectors.md\`` → `\`references/waist-detectors.md\``.
For a router-template reference from a sub-skill: `.claude/skills/pharosone/templates/X` → `../pharosone/templates/X` (sibling-relative — valid once all skills are siblings under one directory, which holds both today under `.claude/skills/` and after Task 6 under `skills/`).
Leave genuine prose mentions (e.g. the `## File layout to create` block in `PIPELINE_DESIGN.md`) as illustrative text — do not turn documentation of the tree into a broken link.

- [ ] **Step 3: Verify only intentional prose remains**

Run: `grep -rn "\.claude/skills" .claude/skills`
Expected: only the illustrative file-tree/prose lines (no path used as an actual "read this file" instruction).

- [ ] **Step 4: Commit**

```bash
git add .claude/skills
git commit -m "refactor(skills): make intra-skill asset paths self-relative"
```

---

### Task 6: Relocate the 6 skills to canonical `skills/` + `.claude/skills` symlink

**Files:**
- Move: `.claude/skills/*` → `skills/*`
- Create: `.claude/skills` symlink → `../skills`

- [ ] **Step 1: Move the skill folders to a top-level `skills/`**

```bash
mkdir -p skills
git mv .claude/skills/pharosone skills/pharosone
git mv .claude/skills/classify-agent-topology skills/classify-agent-topology
git mv .claude/skills/find-agent-seams skills/find-agent-seams
git mv .claude/skills/generate-agent-shim skills/generate-agent-shim
git mv .claude/skills/build-run-profile skills/build-run-profile
git mv .claude/skills/validate-and-certify skills/validate-and-certify
```

- [ ] **Step 2: Replace `.claude/skills` with a symlink to the canonical dir**

```bash
rmdir .claude/skills 2>/dev/null || true
ln -s ../skills .claude/skills
git add .claude/skills
test -f .claude/skills/pharosone/SKILL.md && echo "symlink resolves" || echo "BROKEN SYMLINK"
```
Expected: `symlink resolves`

- [ ] **Step 3: Confirm native Claude Code discovery + no double-discovery from the CLI**

Reproduce a CLI install from a clean clone of this branch into a throwaway target and count installed skills:

```bash
SB="$(mktemp -d)"; git clone -q --branch feat/distributable-skill-packaging . "$SB/src"
cd "$SB/src" && npx --yes skills add . -a claude-code -g --yes 2>&1 | tail -20
ls ~/.claude/skills | grep -E "pharosone|classify-agent-topology|find-agent-seams|generate-agent-shim|build-run-profile|validate-and-certify" | sort | uniq -c
cd - >/dev/null
```
Expected: each of the 6 skills listed **once** (count `1`), the router `pharosone` present.

If the router is missing or any skill appears duplicated, apply the fallback: remove the `.claude/skills` symlink (`git rm .claude/skills`) and add a README note that in-repo Claude Code users run `npx skills add . -a claude-code -g` once; re-run this step and confirm 6 unique skills discovered from `skills/` alone.

- [ ] **Step 4: Run the whole suite (paths changed under the skills tree)**

Run: `uv run pytest -q`
Expected: PASS (the validator test computes paths from the package, not the skill tree; nothing should break).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor(skills): relocate to canonical top-level skills/ with .claude/skills symlink"
```

---

### Task 7: Add the Cursor plugin + marketplace manifests

**Files:**
- Create: `.cursor-plugin/plugin.json`
- Create: `.cursor-plugin/marketplace.json`

- [ ] **Step 1: Write `.cursor-plugin/plugin.json`**

```json
{
  "name": "pharosone",
  "description": "Guided red-team certification of a real AI agent with the PharosOne Probe Engine — classify topology, find the interception seam, generate the bridge adapter, build the run profile, validate, and run.",
  "version": "0.1.1",
  "author": { "name": "PharosOne", "email": "dmitry@pharosone.ai" },
  "skills": "skills"
}
```

- [ ] **Step 2: Write `.cursor-plugin/marketplace.json`**

```json
{
  "name": "pharosone-marketplace",
  "owner": { "name": "PharosOne", "email": "dmitry@pharosone.ai" },
  "plugins": [
    {
      "name": "pharosone",
      "source": ".",
      "description": "Agent red-team certification suite (router + 5 sub-skills)"
    }
  ]
}
```

- [ ] **Step 3: Verify both manifests are valid JSON**

Run:
```bash
python -m json.tool .cursor-plugin/plugin.json >/dev/null && python -m json.tool .cursor-plugin/marketplace.json >/dev/null && echo "both valid JSON"
```
Expected: `both valid JSON`

- [ ] **Step 4: Commit**

```bash
git add .cursor-plugin
git commit -m "feat: add Cursor plugin + marketplace manifests for URL install"
```

---

### Task 8: Harden `.gitignore` so untracked recon can never reach an sdist

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1: Append recon-exclusion rules to `.gitignore`**

Append below the existing "Customer onboarding artifacts" comment block:

```gitignore
# Untracked real-agent onboarding output stays private and out of any build.
# The shipped examples remain tracked via the negations below.
harness/*/
!harness/example-agent/
configs/profiles/*.yaml
!configs/profiles/bank-reset.yaml
!configs/profiles/bridge-http-agent.yaml
!configs/profiles/example-agent.yaml
!configs/profiles/example-agent-p10.yaml
!configs/profiles/finance-support-agent.yaml
!configs/profiles/health-records.yaml
!configs/profiles/healthcare-support-agent.yaml
!configs/profiles/model-tier-example.yaml
```

- [ ] **Step 2: Prove a dirty-tree build excludes untracked recon**

```bash
mkdir -p harness/fake-secret && echo "SECRET RECON" > harness/fake-secret/PASSPORT.md
echo "api_key_env: X" > configs/profiles/fake-secret.yaml
rm -rf dist && uv build --sdist >/dev/null 2>&1
tar tzf dist/*.tar.gz | grep -E "fake-secret" && echo ">>> LEAK <<<" || echo "clean: recon excluded from dirty-tree sdist"
tar tzf dist/*.tar.gz | grep -c "harness/example-agent/"   # examples still ship
rm -rf harness/fake-secret configs/profiles/fake-secret.yaml
```
Expected: `clean: recon excluded from dirty-tree sdist`, then a non-zero count for the example.

- [ ] **Step 3: Commit**

```bash
git add .gitignore
git commit -m "build: gitignore untracked customer recon so it can't enter an sdist"
```

---

### Task 9: Update README + bump version to 0.1.1

**Files:**
- Modify: `README.md` (Quick start / install section, ~lines 47–63)
- Modify: `pyproject.toml` (`version`)

**Interfaces:**
- Consumes: the `probe-engine` console script (published) and the two install channels from Tasks 6–7.

- [ ] **Step 1: Rewrite the README install section**

Replace the "1. Add the skill" block so it documents all channels:

```markdown
**1a. Install the engine** (provides the `probe-engine` CLI the skills call)

```bash
uv add pharosone-security-scanner        # or: pip install pharosone-security-scanner
```

**1b. Add the skills** — pick your agent:

- **Claude Code / any agent** (cross-agent CLI):
  ```bash
  npx skills add pharosone/pharosone
  ```
- **Cursor** (native plugin): Settings → Plugins → Import, paste
  `https://github.com/pharosone/pharosone`.

The skill files are portable; the certification *run* uses the `probe-engine` package from step 1a.
```

- [ ] **Step 2: Bump the package version**

In `pyproject.toml`: `version = "0.1.0"` → `version = "0.1.1"`.

- [ ] **Step 3: Verify the CLI still self-describes and the suite is green**

Run:
```bash
uv run probe-engine validate-artifacts --help >/dev/null && echo "cmd ok"
uv run pytest -q
```
Expected: `cmd ok`, then the full suite PASSES.

- [ ] **Step 4: Commit**

```bash
git add README.md pyproject.toml
git commit -m "docs: document both install channels; bump version to 0.1.1"
```

---

## Out of scope (follow-ups, not this plan)

- Publishing 0.1.1 to PyPI — separate, explicit-go step (same clean `git archive` build + account/project token as 0.1.0).
- Bundling corpus/frameworks/crosswalks as package data so `probe-engine run` works from a pip install without the repo checkout — a larger, separate decision; only `validate-artifacts` is made checkout-independent here.
- Listing in a public Cursor marketplace registry.

## Self-review notes

- **Spec coverage:** A → Tasks 1–4; B → Tasks 4–5; C → Tasks 6–7; D → Task 8; E → Task 9. Double-discovery risk → Task 6 Step 3 with a concrete fallback.
- **Type consistency:** the CLI command (Task 2) consumes exactly the names Task 1 produces (`load_artifact`, `validate`, `ArtifactError`); `ArtifactKind.value` is passed to `validate(kind, instance)` whose first arg is a `str`.
- **No placeholder** steps: every code step carries the code; every check states expected output.
