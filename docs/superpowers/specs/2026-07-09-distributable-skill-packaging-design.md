# Distributable skill packaging — design

- **Date:** 2026-07-09
- **Status:** Approved (design); pending spec review → implementation plan
- **Owner:** onboarding skill suite (`pharosone` + 5 sub-skills)

## Context / Problem

The `pharosone` onboarding suite (router + `classify-agent-topology`, `find-agent-seams`,
`generate-agent-shim`, `build-run-profile`, `validate-and-certify`) is shipped as an Agent-Skills
package installed with `npx skills add pharosone/pharosone`. In practice it does **not** install
correctly across agents and projects:

1. **The router is skipped.** `npx skills add pharosone/pharosone` installs only the 5 sub-skills;
   the `pharosone` router itself is missing from the install. The router's directory is the only one
   carrying non-`SKILL.md` payload — `scripts/` (with a checked-in `__pycache__`), `schemas/`,
   `templates/`, plus two design `.md` files — and that payload is the likely trip-up. The repo name
   (`pharosone`) equalling the skill name (`pharosone`) is a second suspected trigger; both are to be
   confirmed by reproduction during planning.
2. **Cursor doesn't see the CLI install.** The `skills` CLI writes Cursor skills to
   `.agents/skills/`, but the installed Cursor version scans `.cursor/skills/` (project) and
   `~/.cursor/skills/` (global). Result: installed, but invisible.
3. **Hardcoded paths break outside this repo.** Skill bodies reference
   `.claude/skills/pharosone/scripts/validate_artifacts.py` and `.claude/skills/pharosone/schemas/…`
   (and each skill references its own `references/` by absolute `.claude/skills/…` path). These
   resolve only when the current workspace is the probe-engine checkout.
4. **The suite is coupled to a checkout.** It runs `uv run probe-engine …` and a bundled stdlib
   validator, so even when the skill files are visible, the run stages assume the probe-engine
   codebase is the workspace.

## Goals

- The 6 skills install **correctly and completely** (router included) across Claude Code, Cursor,
  and other agents, in **any** project, via two channels:
  - `npx skills add pharosone/pharosone` (the cross-agent CLI), and
  - native **Cursor Plugin** import by URL (`github.com/pharosone/pharosone`).
- **Full decoupling:** the only runtime interface the skills depend on is the `probe-engine`
  console script (pip-installed) plus templates the skill carries and references **self-relatively**.
  Zero hardcoded `.claude/skills/…` paths.
- A **single canonical source** for the skills that serves all three consumers.
- Versioned manifests and install docs covering both channels + the engine install.

## Non-goals (YAGNI)

- Publishing `pharosone-security-scanner` to PyPI or listing in a public Cursor marketplace registry.
  Git-based install (`uv add "… @ git+https://github.com/pharosone/pharosone"`) is sufficient now.
- Changing the certification behaviour, corpus, oracles, or report format.
- Windows-first support; symlink fragility on Windows is documented, not solved.

## Key facts (verified)

- Package: `pharosone-security-scanner` v0.1.0, `requires-python >=3.12`, build-system present.
- Console script already exists: `probe-engine = probe_engine.cli:app` (`pyproject.toml [project.scripts]`).
- GitHub repo: `pharosone/pharosone` — matches both the `npx skills` slug and the Cursor URL.
- The validator `validate_artifacts.py` is **stdlib-only** and reads `schemas/*.json` at runtime —
  cleanly relocatable into the package.
- Hardcoded-path blast radius (to fix): `validate-and-certify/SKILL.md` (lines 44–45, 48–49, 150),
  `pharosone/PIPELINE_DESIGN.md` (lines 33–34, 156), `find-agent-seams/SKILL.md` (line 41,
  self-reference to its own `references/`).

## Design

### A. Absorb the artifact validator into the engine (decoupling)

- Add CLI subcommand **`probe-engine validate-artifacts <passport|seams> <path>`** to
  `src/probe_engine/cli.py` (distinct from the existing corpus/framework/crosswalk `validate`).
- Move the validator + schemas into the package:
  - `src/probe_engine/onboarding/validate.py` — the logic from `validate_artifacts.py`.
  - `src/probe_engine/onboarding/schemas/{passport,seams}.schema.json` — loaded via
    `importlib.resources` and shipped as package data (`pyproject.toml` package-data / include).
- Delete `scripts/` and `schemas/` from the skill directory. The router dir then carries only
  `SKILL.md`, `SEAM_PIPELINE.md`, `PIPELINE_DESIGN.md`, `templates/` — lighter, and expected to fix
  root cause #1.
- Rewrite the references in `validate-and-certify/SKILL.md` and `PIPELINE_DESIGN.md` to call
  `probe-engine validate-artifacts …` instead of `python .claude/skills/pharosone/scripts/…`.

### B. Self-relative skill assets

- `templates/` stays inside the router skill (they are model-read generation assets). Reference them
  as "the `templates/` directory alongside this skill", not by absolute `.claude/skills/…` path.
- Audit **every** intra-skill absolute path (e.g. `find-agent-seams` → its own `references/…`) and
  make it self-relative so it resolves wherever the skill is installed.

### C. Canonical layout + both manifests

- **Canonical skills location: top-level `skills/`.** Move all 6 skill folders from `.claude/skills/`
  to `skills/`.
- `.claude/skills` becomes a **symlink → `../skills`** so in-repo Claude Code still discovers them
  natively. (Dev convenience only; end-users get real copies via the CLI/plugin, no symlink involved.)
- Repo-root **`.cursor-plugin/plugin.json`** (`name: pharosone`, `version: 0.1.0`, description,
  author, `"skills": "skills"`).
- Repo-root **`.cursor-plugin/marketplace.json`** (single entry → the `pharosone` plugin) for the
  URL-import / marketplace path.

### D. Packaging hygiene

- `.gitignore` `__pycache__/` under the skills tree; ensure no compiled/binary artifacts ship inside
  skill folders.
- Keep manifest `version` in lockstep with `pyproject.toml` (`0.1.0`).

### E. Docs

- README "Quick start": document **both** install channels (npx skills; Cursor Settings → Plugins →
  Import by URL) and the engine install (`uv add`/`pip install … @ git+https://github.com/pharosone/pharosone`)
  so `probe-engine` is on PATH. State plainly: the skill is portable, but the *run* stages need the
  engine package present.

## Layout decision & known risk

Chosen: **canonical `skills/` + `.claude/skills` symlink** (over "keep `.claude/skills` + point the
manifest at it" and over "generate the mirrors from a canonical source").

**Risk — double discovery:** `npx skills` searches both `skills/` and `.claude/skills/`; with the
symlink, each skill may be found twice. Resolution during planning: confirm the CLI dedupes by skill
`name`; if it does not, drop the symlink (accept that in-repo native Claude Code discovery then relies
on a CLI/global install) or add a discovery-ignore. This does not affect end-user installs.

## Component boundaries

- **Engine CLI (`probe-engine validate-artifacts`)** — input: artifact kind + file path; output:
  exit code + human-readable pass/fail; depends on package-shipped schemas. Testable in isolation.
- **Skill bodies** — depend only on `probe-engine` (PATH) and self-relative `templates/`. No
  filesystem coupling to a specific checkout.
- **Manifests (`.cursor-plugin/*`)** — declarative; depend only on the `skills/` tree existing.

## Verification plan

1. Reproduce `npx skills add pharosone/pharosone` from a clean clone into a temp target; assert **all
   6** skills (router included) land in `.claude/skills/` and, with `-a cursor -g`, a Cursor-scanned
   dir. Confirm no duplicates from the symlink.
2. `probe-engine validate-artifacts passport harness/el-relocator-qualifier/PASSPORT.md` and
   `… seams harness/el-relocator-qualifier/SEAMS.md` pass (regression against the existing artifacts).
3. Manual: Cursor Settings → Plugins → Import `https://github.com/pharosone/pharosone` parses the
   manifest and installs the plugin; `/pharosone` is invokable.
4. `uv run pytest` — existing 783 tests green; add a unit test for `validate-artifacts` (valid +
   invalid fixture).
5. `grep -rn '\.claude/skills' skills/` returns nothing load-bearing (only intentional prose).

## Rollout / migration

- Single PR on a feature branch. No behavioural change to runs; only packaging, a new CLI subcommand,
  and doc updates. Existing in-repo workflow (`/pharosone <path>` from the probe-engine checkout)
  keeps working through the `.claude/skills` symlink.
