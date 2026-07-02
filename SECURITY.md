# Security Policy

PharosOne Security Scanner is a **behavioral vulnerability scanner / red-team tool for AI
agents**. It runs a versioned corpus
of attack "probes" against a target agent, collects Evidence, and maps it onto the
**AIUC-1** control standard. Because it is an *offensive security tool*, please read the
Responsible Use section before running it, and use the coordinated disclosure process below
to report a vulnerability **in the tool itself**.

Project website: **https://pharosone.ai**

---

## Responsible use

This is an **offensive security / red-team tool**. It is designed to make an AI agent
misbehave so that you can measure and fix the weakness. That capability is powerful and
must be used responsibly.

By using this software you agree that:

- **Authorized targets only.** You will run the Security Scanner **only** against AI agents that
  you own, operate, or are **explicitly authorized in writing** to test. Never point it at a
  third party's agent, API, or infrastructure without permission.
- **You are responsible for compliance.** You are solely responsible for ensuring your use
  complies with all applicable laws and regulations, and with the terms of service and
  acceptable-use policies of any model provider, hosting platform, or upstream API you
  exercise during a run (including in the `model` and `bridge` tiers, which call real LLMs
  and real agents).
- **Scope and blast radius.** Runs on the `model`/`bridge` tiers can trigger real tool calls
  in the agent under test. Point them at non-production or sandboxed environments, and use
  the bridge adapter's side-effect neutralization so that destructive tool calls are
  observed, not actually executed.
- **No weaponization.** Do not use this project, or techniques derived from it, to attack
  systems you are not authorized to test, or to develop or distribute malware.

Defaults are conservative on purpose: an out-of-the-box run executes **offline on the
deterministic `mock` tier** — no network, no API key, no real agent. Testing against real
models or real agents is strictly opt-in via the `model` / `bridge` tiers.

---

## Threat model & scope of the corpus

Understanding what the corpus *is* — and what it is *not* — is important for handling it
safely.

### The corpus is request/prompt-based

The attack corpus (`corpus/probes/*.yaml`, 118 probes) is **request/prompt-based**: each
probe *asks* a target agent to misbehave (via a crafted prompt, a poisoned ingestion
channel, a multi-turn escalation, etc.) and then scores whether the agent complied. The
project **ships no novel working exploits** and no self-propagating code. Attack *techniques*
are adapted from published open-source security research (see `NOTICE`; ~58 probes derive
from NVIDIA's [garak](https://github.com/NVIDIA/garak), plus PharosOne-original, AgentDyn,
MCPTox, and agent_threat_bench sources). The engine and corpus are deliberately
**agent-agnostic** — no probe names a specific customer agent.

### Oracle comparison artifacts are never written to output

Two oracles compare agent behavior against a **local reference artifact that is never
persisted into a score, evidence, or transcript**:

- **`prompt_leak`** compares the agent's reply against a *protected reference string* you
  supply (`protected_snippets`). The reference itself is a local comparison string only.
- **`code_pattern`** scans a code-bearing tool-call argument against a *curated
  malicious-code pattern set*. That pattern set is a local comparison artifact only; it is
  never written into score/evidence and it scans only code-bearing tool-call argument
  fields, not metadata.

### Run logs and reports can be sensitive — by design

When these oracles **fire**, the *proof* is the agent's own output:

- A run configured with **`protected_snippets`** that produces a `prompt_leak` hit will, by
  design, contain the agent's **leaked reply** in the evidence transcript — because that
  leaked text is the proof of the finding.
- A run using the **coding/deploy threat pack** that produces a `code_pattern` hit will, by
  design, retain the **agent-written malicious code** in the recorded tool call — again,
  because it is the proof. This payload is intentionally **not redacted**.

Therefore, treat the following as **sensitive artifacts**:

- run logs (`.eval` files),
- generated JSON/Markdown reports,

whenever the run used `protected_snippets` and/or the coding/deploy pack. Store, transmit,
and share them with the same care as any other sensitive security finding, and scrub or
access-control them before distribution.

### Secrets handling

API keys and system prompts are held **in memory for the duration of a run only** — they are
never written to disk or logs. A run-level `api_key` is passed directly to the model client
rather than through the environment, and the `prompt_leak` protected reference and
`code_pattern` pattern set are excluded from any on-disk output (including resume
checkpoints). If you discover a path by which a secret, protected reference, or comparison
artifact *does* leak to disk, a log, or a report, please report it as a vulnerability (see
below).

---

## Reporting a vulnerability in the tool

If you discover a security vulnerability **in the Security Scanner itself** — for example a
secret-leakage path, a sandbox/side-effect-neutralization escape, an unintended
code-execution or SSRF vector in the engine, web UI, or bridge tier, or a way the tool could
harm systems beyond its intended target — please report it privately.

- **Email:** **security@pharosone.ai**
- **Please do not open a public GitHub issue, pull request, or discussion** for a suspected
  vulnerability. Public disclosure before a fix is available puts users at risk.
- For general (non-security) questions, use **dmitry@pharosone.ai** or the project's public
  issue tracker instead.

**What to include** (as much as you can):

- a description of the issue and its impact,
- the version / commit you tested,
- clear reproduction steps or a proof of concept,
- any relevant logs or configuration (with secrets and sensitive payloads redacted).

**What to expect:**

- We aim to **acknowledge** your report within **3 business days**.
- We will provide an initial assessment and a remediation plan, and keep you updated on
  progress toward a fix.
- We follow **coordinated disclosure**: we ask that you give us a reasonable period
  (typically **90 days**) to release a fix before any public disclosure, and we will work
  with you on timing. We are happy to **credit** reporters in the release notes unless you
  prefer to remain anonymous.

Please act in good faith: only test against your own installation, avoid privacy violations
and service disruption, and do not access or modify data that is not yours while
investigating.

---

## Supported versions

Security fixes are provided for the latest release line. The project is pre-1.0; APIs and the
corpus may change between minor versions.

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |
| < 0.1   | :x:                |

We recommend always running the most recent `0.1.x` release.

---

## License

PharosOne Security Scanner is released under the **Apache License 2.0**. See `LICENSE` and
`NOTICE` for details and third-party attributions.
