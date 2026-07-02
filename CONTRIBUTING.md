# Contributing to the PharosOne Security Scanner

Thanks for your interest in improving the PharosOne Security Scanner — the open-source
behavioral vulnerability scanner for AI agents.
Home: **https://pharosone.ai**

This document explains how to set up the project, run the offline test suite, and extend the
four things that are deliberately decoupled from the engine code: **attacks**, **standards**,
**target frameworks**, and **taxonomy mappings**. Please also read
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) and [`SECURITY.md`](SECURITY.md) before opening a
pull request.

> **Authorized testing only.** This is an offensive-security / red-team tool. Only run it
> against agents you own or are explicitly authorized to test. Contributions that add attack
> techniques must serve defensive evaluation and comply with the Code of Conduct. If you
> discover a vulnerability in *this project*, do **not** open a public issue — follow the
> private process in [`SECURITY.md`](SECURITY.md).

---

## Table of contents

1. [Local setup](#1-local-setup)
2. [Running the test suite](#2-running-the-test-suite)
3. [Validating the corpus](#3-validating-the-corpus)
4. [Extension axes](#4-extension-axes)
   - [Add an attack (a probe)](#41-add-an-attack-a-probe)
   - [Add a standard](#42-add-a-standard)
   - [Add a target framework](#43-add-a-target-framework)
   - [Add a taxonomy mapping](#44-add-a-taxonomy-mapping)
5. [Pull request expectations](#5-pull-request-expectations)
6. [Secrets and safety](#6-secrets-and-safety)
7. [Getting help](#7-getting-help)

---

## 1. Local setup

The project targets **Python >= 3.12** and is managed with [**uv**](https://docs.astral.sh/uv/).

```bash
# Clone your fork, then from the repo root:
uv sync --extra dev          # create the venv and install runtime + dev dependencies
```

That is the whole setup. There is no separate lint step configured, and no service, key, or
Docker daemon is required for day-to-day development — every default path runs **offline** on
the deterministic `mock` tier.

The CLI is exposed as `probe-engine`:

```bash
uv run probe-engine --help          # validate / run / report / serve
uv run probe-engine serve           # optional FastAPI UI at http://127.0.0.1:8000
```

---

## 2. Running the test suite

```bash
uv run pytest                       # run the full suite
uv run pytest tests/scoring/test_oracle.py::test_name   # a single test
uv run pytest tests/run -q          # one package
```

**The suite must stay fully offline and green.** As of this writing it is ~792 tests, and it is
a hard invariant that they run with **no network, no API keys, and no Docker** — the entire
suite exercises the `mock` tier and stubs. A contribution that makes a test reach the network,
require a real model, or depend on wall-clock time or global randomness will be sent back.

Practical rules when adding tests:

- Route all randomness through an explicit, seeded `random.Random` — never the global `random`
  module and never the wall clock. Variation and obfuscation are byte-reproducible for a fixed
  seed, and tests rely on that.
- Do not call real model providers. If you are testing a judge/attacker path, use the offline
  fallbacks and the `mock` tier.
- New behavior needs a test. Bug fixes should come with a regression test that fails before the
  fix.

---

## 3. Validating the corpus

Any change to a probe, the framework, or the crosswalk should pass the offline consistency
check before you push. This loads and schema-validates every probe (rejecting duplicate ids and
malformed specs), loads the framework and crosswalk, and confirms the crosswalk only references
controls that exist in the framework:

```bash
uv run probe-engine validate \
  --corpus corpus/probes \
  --framework frameworks/aiuc-1.yaml \
  --crosswalk crosswalks/aiuc-1/crosswalk.yaml
```

A good end-to-end smoke test after data edits is to also run the corpus offline against the
mock target and skim the report:

```bash
uv run probe-engine run \
  --corpus corpus/probes \
  --framework frameworks/aiuc-1.yaml \
  --crosswalk crosswalks/aiuc-1/crosswalk.yaml \
  --out reports/out
# writes reports/out/report.json and reports/out/report.md
```

---

## 4. Extension axes

Data is decoupled from code. The engine and the corpus are deliberately **agent-agnostic** — no
probe and no core module may name a specific customer or agent. There are four independent ways
to extend the system.

### 4.1 Add an attack (a probe)

An attack is a single YAML file in `corpus/probes/`. The loader validates it against a strict
Pydantic schema, so start from an existing probe of the same shape (e.g.
`corpus/probes/extraction-system-prompt.yaml` for a single-turn conversation attack, or
`corpus/probes/indirect-status-via-record.yaml` for an indirect injection over an ingestion
channel).

A probe carries:

- `id` (unique across the corpus), `version`, `title`, `severity`, and a human-readable `intent`.
- `taxonomy_tags` — one or more coordinates in `atlas` / `owasp_agentic` / `cwe`. These are how
  the probe maps onto controls via the crosswalk. An optional `control_overrides` list can
  credit a specific control that no clean public coordinate maps to (with a `rationale`).
- `applicability` — the gating facts: `required_tools` (canonical **capabilities**, never
  concrete tool names), `industries`, `languages`, and the `requires_identity_context` /
  `requires_lifecycle_context` flags.
- `scenario` — `single_turn`, `chain`, or `adaptive`, with turns that carry `seed_prompts`
  and/or `poison`. A poison turn that arrives over an ingestion surface must declare its
  `channel` (one of the canonical channels, e.g. `ingested_record`, `retrieved_doc`,
  `tool_result`, `memory`, `file_content`, `image_content`; `message`/`history` are universal).
- `variation` — strategy (`deterministic` is the default and the certification default) and
  `n_variants`.
- `evaluation` — the schema **requires** at least one of a `binary` oracle check and a
  `semantic` judge check (most probes carry both). Oracles are the structural floor; the judge
  is the authoritative ceiling. Pick an oracle kind that actually matches the end state you are
  testing (there are 14: `tool_called`, `tool_arg`, `secret_leaked`, `state_changed`,
  `authz_violation`, `state_invariant`, `prompt_leak`, `contains`, `arg_injection`,
  `code_pattern`, `output_pattern`, `cross_turn_contradiction`, `runaway_output`,
  `secret_fragment`).
- `expected_behavior` — a one-line description of the safe outcome.
- `provenance` — **required**. See below.

#### Provenance block (required, and it must be honest)

Every probe records where its technique comes from. Be truthful about the source — this feeds
the top-level `NOTICE` file and the audit report.

```yaml
# A technique adapted from open-source research:
provenance:
  source: "garak (NVIDIA, Apache-2.0)"
  url: "https://github.com/NVIDIA/garak/blob/main/garak/probes/dan.py"
  harvested_as: corpus_yaml
```

```yaml
# An original PharosOne probe:
provenance:
  source: "PharosOne (original)"
  disclosure_id: "novel-extraction-2026"   # optional
  harvested_as: corpus_yaml
```

- `source` (required) — an honest attribution string. If a probe adapts a technique from
  upstream research, name the project and its license (e.g. `garak (NVIDIA, Apache-2.0)`).
  Original work is `PharosOne (original)`. Other cited sources in the corpus include
  `AgentDyn`, `MCPTox`, and `agent_threat_bench`.
- `url` (optional) — a link to the upstream technique, when there is one.
- `disclosure_id` (optional) — an internal/disclosure reference.
- `harvested_as` — defaults to `corpus_yaml`.

We adapt the *technique and a representative payload* and re-express it in the engine's own
format and voice — we do **not** vendor or redistribute upstream payload datasets. If your probe
approximates the upstream (a different modality, a retargeted oracle), add a short `fidelity:`
note inside the `source` string, consistent with the existing `NOTICE` guidance.

#### Two principles a probe must respect

- **Agent-agnostic.** Target canonical capabilities (e.g. `status_change`, `deploy`) and
  canonical channels — never a concrete customer tool name, and never a customer or product
  name anywhere in the probe. The same corpus must test every agent.
- **Blind spots are never silent passes.** If a probe requires a capability, channel, or
  identity/lifecycle context that a target does not declare, selection **skips and surfaces**
  it as a blind spot — it is never re-routed onto another channel or counted as robust. Design
  your `applicability` and channels so that a target that genuinely can't be tested is gated
  *out*, not falsely passed.

After adding or editing a probe, run the [`validate`](#3-validating-the-corpus) command and the
test suite.

### 4.2 Add a standard

A control standard is data, not code:

- Add the framework as `frameworks/<std>.yaml` (controls with their real, verbatim wording —
  control text is taken from the source standard, never invented; controls that aren't
  behaviorally testable are marked accordingly so the report says `not_testable`, never
  "failed").
- Add its crosswalk directory `crosswalks/<std>/` (see the next section for entry shape).

Then run `validate` against your new framework + crosswalk. The AIUC-1 pair
(`frameworks/aiuc-1.yaml` + `crosswalks/aiuc-1/`) is the worked reference.

### 4.3 Add a target framework

There are two levels here.

**Common case — a bridge adapter (no engine change).** Most real agents are integrated at the
`bridge` tier: you write an adapter that cuts the agent at its narrowest reachable waist so every
tool call is **observable**, every untrusted channel is **injectable**, and real side effects are
**neutralized**. The adapter exposes `external()`, declares which `channels()` it can route
poison into, and maps the agent's concrete tools to canonical capabilities.
`harness/example-agent/adapter.py` is a worked example, and
`examples/bridge_real_agent.py` is the framework-agent template. These live outside the engine
package, so they don't require core changes.

**Rare case — a genuinely new execution backend.** A new *tier* lives in
`src/probe_engine/targets/` (alongside `mock.py`, `model.py`, `bridge.py`) and is wired into
`targets/registry.build_target_solver`, which returns an Inspect solver per probe + tier. Keep
the `mock` tier as the offline default that the whole test suite exercises. Whatever you add
must respect the blind-spot guard: if an oracle can never be adjudicated on a target, the probe
is skipped and surfaced, never crashed and never manufactured into a pass.

### 4.4 Add a taxonomy mapping

To connect an existing taxonomy coordinate to one or more controls, add a **crosswalk entry**
under the relevant `crosswalks/<std>/` directory. A crosswalk maps a coordinate
(`atlas` / `owasp_agentic` / `cwe`) to controls, with an evidence type and a
human-verification flag. Lookups are exact-coordinate (no prefix matching), which preserves
granularity. Crosswalk entries are research-derived and should be flagged for subject-matter-
expert review; never invent a mapping you can't justify. Run `validate` afterward — it confirms
every referenced control actually exists in the framework.

---

## 5. Pull request expectations

- **Tests pass and stay offline.** `uv run pytest` must be green with no network/keys/Docker.
  New behavior ships with tests; bug fixes ship with a regression test.
- **Data edits are validated.** If you touched `corpus/probes/`, `frameworks/`, or
  `crosswalks/`, run the [`validate`](#3-validating-the-corpus) command and include the fact that
  it passed.
- **Conventional-commit-style messages.** Use the `type(scope): summary` form, matching the
  existing history — e.g. `feat(corpus): add cross-session memory-leak probe`,
  `fix(scoring): correct Wilson upper-bound edge case`, `docs(architecture): clarify judge pass`,
  `chore(profiles): refresh example inventory`.
- **Keep the corpus agent-agnostic.** No customer or product names in probes or core modules.
- **Keep secrets out** (see below).
- **Scope your PR.** Prefer focused changes. If you're adding a probe, one probe per file; if
  you're adding a standard, keep framework and crosswalk in the same PR so `validate` stays
  green.
- **Be respectful and responsible.** All contributions are governed by
  [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md). Attack content is for authorized defensive testing
  only.

By contributing, you agree that your contributions are licensed under the project's
**Apache-2.0** license (see `LICENSE` and `NOTICE`).

## 6. Secrets and safety

Secrets are **in-memory for the run only** — never write API keys or system prompts to disk,
logs, checkpoints, evidence, or the report. A run-level `api_key` is threaded through the model
provider directly rather than the environment. The `prompt_leak` oracle's protected reference
and the `code_pattern` oracle's malicious-pattern list are **local comparison artifacts** and
must never be written into a score, evidence, or transcript. When such an oracle *fires*, the
agent's own leaked text or malicious code is in the evidence **by design** — it is the proof —
so reports and `.eval` logs from runs configured with protected snippets or the coding/deploy
pack are sensitive; treat and handle them accordingly. There are regression tests pinning these
invariants; do not weaken them.

Never commit real credentials, customer data, or a customer's system prompt. If you need
fixtures, synthesize inert placeholder values.

## 7. Getting help

- General questions and contributions: **dmitry@pharosone.ai**
- Security disclosures (do not use public issues): **security@pharosone.ai** — see
  [`SECURITY.md`](SECURITY.md)
- Project site: **https://pharosone.ai**
- Architecture deep-dive: [`docs/architecture.md`](docs/architecture.md); design specs live
  under `docs/design/`.

Welcome aboard, and thank you for helping make AI agents safer.
