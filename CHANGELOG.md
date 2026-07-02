# Changelog

All notable changes to the PharosOne Security Scanner are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-07-02

First public release.

### Added

- **Behavioral vulnerability scanner for AI agents.**
  It runs a versioned corpus of attack "probes" against a target agent, collects Evidence, and produces
  an audit-ready report. Website: <https://pharosone.ai>.
- **Agent-agnostic probe corpus** — 118 attack probes as versioned YAML specs, decoupled from engine
  code. No probe names a specific target agent. Includes a coding/deploy threat pack (unreviewed/
  unauthorized deploy, supply-chain backdoors, CI gate tampering) and cross-session memory campaigns.
- **AIUC-1 control mapping** — Evidence is mapped onto the AIUC-1 control standard through a research-derived
  crosswalk to ATLAS, OWASP Agentic, and CWE taxonomies. Controls that are not behaviorally testable are
  marked `not_testable` rather than "failed".
- **Three target tiers behind one solver interface** — `mock` (deterministic, offline, no API key — the
  default), `model` (a synthetic agent on a real LLM plus sandbox tools), and `bridge` (a real external
  agent over an OpenAI-compatible HTTP endpoint or an async callable).
- **Judge-only success decision** — a per-trial oracle stashes provisional evidence, then a post-eval
  batch judge pass adjudicates each probe's trials and overwrites the verdict. When no judge model
  resolves, the batch pass is a no-op and success falls back to the deterministic oracle hit. A
  configured-but-unavailable judge degrades loudly (never a silent pass).
- **Blind-spot surfacing** — probes whose required capability, delivery channel, or context the target
  does not declare are skipped and surfaced, never re-routed or counted as robust. Selection is an
  execution parameter, not a report filter.
- **14 oracle kinds** — `tool_called`, `tool_arg`, `secret_leaked`, `state_changed`, `authz_violation`,
  `state_invariant`, `prompt_leak`, `contains`, `arg_injection`, `output_pattern`,
  `cross_turn_contradiction`, `runaway_output`, `secret_fragment`, and `code_pattern`.
- **Capability and channel selection** — probes target canonical capabilities (not concrete tool names)
  and abstract channels (message, history, and declarable ingestion surfaces), gating which probes run
  against a given agent's declared inventory.
- **Cost-aware LLM planner** — folds a per-trial cost weight (by scenario type and turn count) into
  trial-budget allocation, with an optional per-probe cost cap; the deterministic path is unaffected.
- **Turn-by-turn scenarios** — `single_turn`, `chain`, and `adaptive` run for real (with early stop when
  the oracle fires), rather than being flattened into a single prompt.
- **CLI** (`probe-engine`) with `validate` and `run` subcommands, plus opt-in resume via per-probe
  checkpoints. Config-driven runs via example profiles under `configs/profiles/`.
- **FastAPI web UI** (`probe-engine serve`, <http://127.0.0.1:8000>) with live SSE run progress.
- **Bridge-adapter onboarding skills** — a sequenced skill set (classify topology, find seams, generate
  shim, build profile, validate and certify) that wires a real customer agent into a ready-to-certify
  bridge setup, with `harness/example-agent/` as a worked example.
- **JSON and Markdown reports** with ASR and Wilson confidence intervals.
- **Secrets handling** — API keys and system prompts stay in-memory for the run only; the `prompt_leak`
  reference and `code_pattern` regex set are local comparison artifacts, never written into scores,
  evidence, or transcripts.
- **Fully offline test suite** — 783 tests requiring no network, API keys, or Docker.

[Unreleased]: https://github.com/pharosone/pharosone/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/pharosone/pharosone/releases/tag/v0.1.0
