<div align="center">

# PharosOne Security Scanner

**Point it at your AI agent. It wires itself in, red-teams it, and hands you an audit-ready report.**

[**Website**](https://pharosone.ai) · [Architecture](docs/architecture.md) · [Experiments](benchmarks/variation_strategies/README.md) · [Design specs](docs/design/)

![License](https://img.shields.io/badge/license-Apache--2.0-blue)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![Probes](https://img.shields.io/badge/attack%20probes-118-green)
![Standard](https://img.shields.io/badge/maps%20to-AIUC--1-8A2BE2)

</div>

> [!TIP]
> The fastest way in is the **`pharosone` skill**. Run it in Claude Code, point it at your agent's
> repository, and it does the wiring for you — investigate → wrap → scan → report. Jump to
> [Quick start](#-quick-start).

---

## Overview

Traditional scanners check configuration and code. An AI agent's most dangerous failures are
**behavioral** — it can be talked into leaking its system prompt, calling a dangerous tool with
attacker-controlled arguments, trusting an instruction smuggled inside a document it ingested, or
laundering a record's status past a decision gate. None of that shows up in a static scan; it only
appears when you *drive the agent and watch what it does*.

PharosOne Security Scanner runs a versioned, **agent-agnostic** corpus of attack **probes** (YAML),
mutates and repeats each one against your agent, decides success with a deterministic oracle backed
by an LLM judge, and maps the evidence onto the **AIUC‑1** control standard through an
ATLAS / OWASP‑Agentic / CWE crosswalk — producing an audit-ready report with attack success rates,
confidence intervals, and surfaced blind spots.

> **Authorized testing only.** This is an offensive-security tool. Use it exclusively against agents
> you own or have explicit written permission to test. See [Responsible use](#responsible-use).

---

## 🚀 Quick start

You don't hand-write test harnesses. The **`pharosone` skill** onboards a real agent for you as a
guided conversation.

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

> [!NOTE]
> The repo also ships **`setup-dialog-streaming`** — a standalone skill that wires a bot codebase to
> stream its conversations (messages **and tool calls**) into the PharosOne dialog-analysis cabinet,
> via the Python / TypeScript / Go SDKs or raw HTTP. Install just that one:
>
> ```bash
> npx skills add pharosone/pharosone --skill setup-dialog-streaming
> ```

**2. Point it at your agent, in Claude Code**

Ask Claude to red-team your agent and give it the path to the agent's repository — for example:

```
Use the pharosone skill to red-team the agent in ../my-support-agent
```

The skill then, as a guided flow:

- **Investigates** the agent — its topology (in-process / local HTTP / remote), tool inventory, and
  untrusted input channels.
- **Asks a few scoping questions** — which standard(s) to certify against, how deep/expensive to go,
  and which models run the target and the attacker. It **never asks for a secret key** — only the
  **environment-variable name** that holds it.
- **Wraps the agent** in a bridge adapter (see below), builds a run profile, validates everything,
  and **launches the scan with live progress**, ending in a JSON + Markdown report.

---

## How it wraps your agent

The scanner only catches "did a thing it shouldn't" when the agent's actions are **observable** and
its untrusted inputs are **injectable**. So the skill generates a **bridge adapter** that cuts the
agent at its **narrowest reachable waist** — above the messy layers (auth / transport /
serialization), below the agent's reasoning — where:

- every **tool call is observable** (the scanner sees exactly what the agent decided to do),
- every **untrusted channel is injectable** (message, retrieved document, tool result, memory, a
  record field, an image, …), and
- real **side effects are neutralized** — writes and outbound actions are recorded, never executed.

You never rewrite the agent; you adapt only its I/O. The adapter exposes `external()`, declares which
`channels()` it can route poison into, and maps the agent's concrete tools to canonical capabilities.
See the worked example in [`harness/example-agent/`](harness/example-agent/) and the framework-agent
template in [`examples/bridge_real_agent.py`](examples/bridge_real_agent.py).

---

## ✨ Architecture & why it's different

A run is a fixed pipeline; the CLI and the web UI both drive the same `run_corpus`:

```
corpus → selection → variation → compile → eval (target) → oracle → judge → aggregate → coverage → report
```

**What sets it apart:**

- **Judge-only success.** The semantic invariant lives in the **oracle** and an **LLM judge**, never
  in the prompt text. A deterministic oracle stamps a provisional per-trial hit; a post-eval batch
  judge then adjudicates *all* of a probe's trials and sets the verdict. This is what makes heavily
  mutated / obfuscated attack rewrites safe to use without breaking scoring.
- **Blind spots are never silent passes.** Selection is an *execution* parameter, not a report
  filter. A probe whose required capability, channel, or context the target doesn't declare is
  **skipped and surfaced** — never re-routed onto another surface and never counted as "robust".
- **Statistical, not anecdotal.** Every probe is repeated and reported as an attack success rate with
  a Wilson confidence interval, not a single lucky jailbreak.
- **Standard-mapped output.** Findings resolve to **AIUC‑1** controls; controls that aren't
  behaviorally testable are marked `not_testable`, never "failed".
- **Data decoupled from code.** Attacks, the standard, and the crosswalk are versioned data — extend
  along one axis at a time (drop a probe YAML, add a standard, add a target adapter).

Deep dives:

- 📐 **Architecture** — [`docs/architecture.md`](docs/architecture.md) (principles, the variation
  subsystem, scoring, standards mapping, with diagrams).
- 🧪 **Experiments** — [`benchmarks/variation_strategies/README.md`](benchmarks/variation_strategies/README.md)
  (empirical comparison of attack-variation policies and LLM-judge calibration).
- 📄 **Design specs** — [`docs/design/`](docs/design/).

---

## Corpus & standards

- **118 attack probes** (`corpus/probes/*.yaml`) — prompt injection & extraction, indirect injection
  over ingestion channels, tool-misuse and authorization gaps, state/status laundering, output
  exfiltration, a coding/deploy threat pack (unreviewed/unauthorized deploy, supply-chain backdoors,
  CI-gate tampering), and cross-session memory campaigns.
- **AIUC‑1** control standard (`frameworks/aiuc-1.yaml`) mapped via a research-derived crosswalk
  (`crosswalks/aiuc-1/`) over ATLAS / OWASP Agentic / CWE coordinates.

Attack techniques are adapted from published open-source research (see [`NOTICE`](NOTICE)); upstream
payload datasets are **not** redistributed.

---

## Running it directly (CLI)

The skill is the recommended path, but the scanner is a normal CLI too (`probe-engine`). Install from
source:

```bash
git clone git@github.com:pharosone/pharosone.git
cd pharosone
uv sync --extra dev
```

**Validate** the corpus, framework, and crosswalk:

```bash
uv run probe-engine validate \
  --corpus corpus/probes --framework frameworks/aiuc-1.yaml \
  --crosswalk crosswalks/aiuc-1/crosswalk.yaml
```

**Run** against a target and write reports to `reports/out/`:

```bash
uv run probe-engine run --profile configs/profiles/finance-support-agent.yaml \
  --corpus corpus/probes --framework frameworks/aiuc-1.yaml \
  --crosswalk crosswalks/aiuc-1/crosswalk.yaml --out reports/out
```

**Serve** the web UI (live run progress):

```bash
uv run probe-engine serve            # http://127.0.0.1:8000
```

See [`configs/profiles/`](configs/profiles/) and [`examples/industries/`](examples/industries/) for
example run profiles.

---

## Requirements — models & keys

Real red-teaming needs **LLM access on both sides**:

- **Target side** — the agent under test runs on its own model (the `bridge` tier reaches your real
  agent; the `model` tier runs a synthetic agent on a real LLM).
- **Attacker side** — an LLM generates the mutated/adaptive attacks and acts as the semantic judge
  that decides success.

Keys are read from the **environment only** and stay in memory for the run — never written to disk or
logs; the skill asks for env-var **names**, never key values.

A deterministic **`mock` tier** needs no model and no key — it drives the whole pipeline offline and
is what the test suite (783 tests) and CI use. It's great for trying the pipeline, corpus
development, and demos, but it does **not** exercise a real agent; use `model`/`bridge` for actual
scanning.

---

## Responsible use

> [!WARNING]
> This is a **dual-use offensive-security tool**. Run it **only** against AI agents you own or are
> explicitly authorized in writing to test. You are responsible for compliance with applicable laws
> and with the terms of any model provider or platform you exercise during a run. Reports from runs
> configured with `protected_snippets` or the coding/deploy pack are **sensitive** — when those
> oracles fire, the agent's own leaked text or malicious code is retained in the evidence *by design*
> (it is the proof). See [`SECURITY.md`](SECURITY.md); report tool vulnerabilities to
> **security@pharosone.ai** (not a public issue).

---

## Contributing

New probes, standards, target adapters, and crosswalk entries all extend cleanly along a single axis.
See [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md). Questions:
**dmitry@pharosone.ai**.

## License

**Apache License 2.0** — see [`LICENSE`](LICENSE). Third-party attributions in [`NOTICE`](NOTICE).

---

<sub>PharosOne Security Scanner · [pharosone.ai](https://pharosone.ai)</sub>
