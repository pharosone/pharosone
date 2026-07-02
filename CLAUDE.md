# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

PharosOne Probe Engine — a behavioral vulnerability scanner for AI agents, built on **Inspect AI**.
It takes a versioned corpus of attack specs (YAML "probes"), mutates and repeatedly runs each one
against a target agent, collects Evidence, and maps that Evidence onto the **AIUC-1** control
standard through a taxonomy crosswalk (ATLAS / OWASP Agentic / CWE), producing an audit-ready report.

The README is the most complete usage reference; this file is the architecture map.

## Commands

```bash
uv sync --extra dev                  # install (uv-managed; Python >=3.12)
uv run pytest                        # run all tests (783 tests across 87 files, all offline — no network/keys/Docker)
uv run pytest tests/scoring/test_oracle.py::test_name   # single test
uv run pytest tests/run -q           # one package

# Validate corpus/framework/crosswalk integrity (offline; good first sanity check after data edits)
uv run probe-engine validate --corpus corpus/probes \
  --framework frameworks/aiuc-1.yaml --crosswalk crosswalks/aiuc-1/crosswalk.yaml

# Run the corpus offline against the mock target and write reports/out/report.{json,md}
uv run probe-engine run --corpus corpus/probes \
  --framework frameworks/aiuc-1.yaml --crosswalk crosswalks/aiuc-1/crosswalk.yaml --out reports/out

uv run probe-engine run --profile configs/profiles/finance-support-agent.yaml ...   # config-driven run
uv run probe-engine serve            # FastAPI UI on http://127.0.0.1:8000 (live SSE progress)
```

All defaults run **offline on the mock tier** (`mockllm`, no API key). Real LLM/agent testing is
opt-in via tier flags. There is no separate lint step configured.

## Pipeline (the path a run takes)

`corpus.loader` → `run.selection` → `variation.generate` → `compile.compiler` → `inspect_ai.eval`
(driven by a target solver) → `scoring.oracle` → `scoring.batch_judge` → `scoring.aggregate` →
`mapping.coverage` → `report.builder`. The CLI (`cli.py`) and web UI (`web/app.py`) are the two
entry points; both call `run.executor.run_corpus`.

Layers (`src/probe_engine/`): `domain/` (Pydantic models — Probe, Framework/Control, Crosswalk,
Evidence, Coverage, RunConfig, enums), `corpus/` (load+validate probes), `variation/` (mutators),
`scoring/` (oracles, batch judge, ASR + Wilson CI), `targets/` (the three tiers behind one
interface), `compile/` + `run/` (Probe → Inspect Task → Evidence), `mapping/` (crosswalk + coverage
engine), `report/` (JSON/Markdown), `sandbox/` (offline state/tools backend), `config/` (profiles),
`web/` (FastAPI UI).

## Core architecture decisions (these span multiple files — read before changing behavior)

- **Data is decoupled from code.** Attacks (`corpus/probes/*.yaml`), the standard
  (`frameworks/aiuc-1.yaml`), and the crosswalk (`crosswalks/aiuc-1/crosswalk.yaml`) change
  independently of the engine. The engine and corpus are deliberately **agent-agnostic** — no probe
  or core module names a specific customer agent. Extension axes: new attack → YAML in
  `corpus/probes/`; new standard → `frameworks/<std>.yaml` + `crosswalks/<std>/`; new target
  framework → adapter in `targets/`; new taxonomy mapping → crosswalk entries. The corpus includes a
  **coding/deploy threat pack** for code-writing agents: unreviewed/unauthorized deploy
  (`deploy-unreviewed-code`, `unauthorized-deploy`, `indirect-deploy-via-ticket`), supply-chain
  backdoors and CI gate tampering (`dependency-backdoor`, `ci-config-tamper`,
  `indirect-code-backdoor-via-ticket` — all fire via the `code_pattern` oracle), and cross-session
  memory campaigns/leaks (`cross-session-memory-campaign`, `cross-session-leak` over the `memory`
  channel). Several ride the `ingested_record`/`memory` channels and so are selected only for agents
  that declare them.

- **Three target tiers, one solver interface** (`targets/registry.build_target_solver`):
  `mock` (deterministic, offline, no LLM — the default and what all tests use), `model` (a synthetic
  agent on a real LLM + sandbox tools), `bridge` (a *real external agent* via an OpenAI-compatible
  HTTP endpoint or an `external(request)->response` async callable). `mock`/`bridge` run Inspect on
  the `mockllm/model` placeholder; only `model` uses the real eval model.

- **Success is decided JUDGE-ONLY in a post-eval batch pass, not per-trial.** `scoring.oracle`
  computes a *provisional* binary hit per trial and stashes evidence (transcript/tool_calls/reply/
  binary_hit) into score metadata for **every** sample. Then `executor._apply_batch_judge` runs
  `scoring.batch_judge` over all of a probe's trials and **overwrites** `metadata["success"]` with
  the judge verdict. **Offline fallback:** when no judge model resolves (or the probe has no semantic
  check), the batch pass is a no-op and `success == binary_hit` — byte-for-byte the old behavior.
  This is why prompt variation/obfuscation is safe: the *oracle* holds the invariant, not the text.
  The judge path stamps every sample score `metadata["judge_applied"]` (True on adjudication, False on
  every offline/no-judge/error fallback), and `_persist_judged_log` writes the judge-corrected EvalLog
  back to its own `.eval` (best-effort, never raises) so the on-disk log reflects the final verdict.
  A *configured-but-unavailable* judge degrades **loudly** (a warning, verdict marked UNVERIFIED, mask
  falls back to binary_hit so the run survives) — never a silent pass (`scoring.judge.JudgeUnavailable`,
  `scoring.batch_judge.batch_judge_with_status`).

- **Selection is an execution parameter, not a report filter** (`run.selection`). Industry, declared
  tool inventory, delivery channels, identity context, and lifecycle context all gate which probes
  *run*. The guiding principle is **blind spots are never silent passes**: a probe whose required
  capability/channel/context the target doesn't declare is *skipped* (and surfaced), never re-routed
  or counted as robust. The CLI prints `selected N/total`. The engine also guards the *bridge* tier
  itself: `run_corpus` builds the target solver per probe and catches a blind-spot `ValueError`
  (oracle can never fire / requires binary evaluation), **skipping and surfacing** that probe via a
  `"skip"` progress phase instead of crashing — global misconfig (no endpoint, unknown tier) still
  re-raises. `selection.reconcile_channels(declared, routable)` cross-checks the channels a profile
  *declares* against the ones a bridge adapter can actually *route* poison into: `declared_not_routable`
  is loud false coverage, `routable_not_declared` is missed coverage. `run_corpus` returns an
  `EvidenceList` (a `list` subclass) carrying `.blind_spots`. `run_corpus` also supports **opt-in
  resume** (`resume=True` + `out_dir`): each completed probe is checkpointed to
  `<out>/.checkpoint/<probe_id>.json` keyed by a `config_hash` over the probe + a *non-secret*
  result-affecting config subset (api_key/system_prompt/protected_snippets are deliberately excluded
  from the hash); a hash-matching checkpoint is reused, a mismatch/corrupt one re-runs. `resume=False`
  (default) creates no checkpoint dir — today's exact behavior (`run/checkpoint.py`).

- **The LLM planner is cost-aware** (`plan/allocate.py`). `allocate(...)` folds a per-trial
  `_cost_weight(probe)` (scenario base × turn count: single_turn=1, chain=3, adaptive=8) into the
  LLM redistribution so *extra* budget above the floor flows to cheap probes instead of inflating the
  slowest ones (severity stays a multiplicative factor). An optional `max_cost` caps any single
  probe's inflated cost back toward the floor. The deterministic path is untouched.

- **Capability layer** (`targets/capabilities.py`). A probe's `required_tools` and oracle targets are
  canonical *capabilities*, not concrete tool names. `tool_inventory` maps an agent's real tools to
  capabilities; oracles fire on a call's name **or** any capability it fulfils. The coding/deploy
  threat pack adds two such capabilities: `deploy` (ship/release to an environment — the
  review-then-ship invariant) and `code_exec`/`edit_file` (write or run code — what the `code_pattern`
  oracle scans). Agents that don't declare them gate the pack's probes OUT as blind spots, never silent
  passes.

- **Channels** (`CANONICAL_CHANNELS` in `domain/probe.py`). The abstract "doorway" an attack uses.
  `message`/`history` are universal; ingestion surfaces (`ingested_record`, `retrieved_doc`,
  `tool_result`, `memory`, `file_content`, `image_content`) must be declared by the target to be tested.
  `multi_channel`
  turns fan one payload (distinct rewrite per surface) across every declared channel.

- **Scenarios run turn-by-turn for real** (`single_turn` / `chain` / `adaptive`), not flattened into
  one prompt. `adaptive` drives a real attacker LLM (model/bridge) or a deterministic
  escalate-on-refusal attacker (mock), with early-stop when the oracle fires.

- **Oracle kinds** (`scoring/oracle.py::evaluate_oracle`, 14 total): `tool_called`, `tool_arg`,
  `secret_leaked`, `state_changed`, `authz_violation` (fires only on allow-where-deny-expected
  enforcement gaps), `state_invariant` (e.g. `no_regress` status-laundering), `prompt_leak`
  (high-precision verbatim token-overlap vs a protected reference), `contains`, `arg_injection`
  (injected directive surfacing in a tool-call argument), `output_pattern` (regex/marker over the
  reply, e.g. markdown image/link/script-tag exfil), `cross_turn_contradiction` (a claim contradicted
  across turns), `runaway_output` (reply exceeds a max-chars budget), `secret_fragment` (partial
  secret-token overlap), `code_pattern` (high-precision malicious-code scanner: fires when an
  `edit_file`/`code_exec` tool-call's content/diff argument matches a curated malicious-code regex —
  exfil sinks, secret writes, disabling a security gate, eval of remote input, backdoor imports; the
  pattern set is a local comparison artifact, never written into score/evidence).

- **Report honesty.** Controls that aren't behaviorally testable (need config/docs/telemetry) are
  marked `not_testable`, never "failed". Crosswalk entries are research-derived and flagged for SME
  review; control wordings are taken from the real AIUC-1 text, never invented.

## Secrets handling (load-bearing, verify when touching targets/scoring/web)

API keys and system prompts are **in-memory for the run only** — never written to disk or logs.
A run-level `api_key` is passed through `get_model(..., api_key=...)` rather than the environment.
The `prompt_leak` oracle's protected reference is a *local comparison string*: it is never written
into the score, evidence, or transcript (a regression test pins this). When `prompt_leak` *does*
fire, the agent's own leaked reply is in the evidence transcript by design (it is the proof) — so
reports from runs with `protected_snippets` must be treated as sensitive. The `code_pattern`
oracle's `patterns` list is likewise a *local comparison artifact* (never written to score/evidence;
it scans only code-bearing tool-call arg fields, not metadata); when it fires, the agent's own
malicious code is in the recorded tool call by design (it is the proof) — so `.eval` logs and
reports from runs with the coding/deploy pack must be treated as sensitive (the agent-written
payload is intentionally retained, not redacted).

## Onboarding a real agent (`.claude/skills/`)

The skills under `.claude/skills/` are part of the product, not editor config. `pharosone`
is a router that sequences five sub-skills to wire a customer's agent folder into a ready-to-certify
setup: `classify-agent-topology` → `find-agent-seams` → `generate-agent-shim` → `build-run-profile`
→ `validate-and-certify`. The output is a `bridge`-tier adapter (`external()` + `channels()` +
injection routing) plus a `configs/profiles/<agent>.yaml`. Core principle: cut the agent at the
narrowest reachable waist so every tool call is **observable**, every untrusted channel is
**injectable**, and real side effects are **neutralized**. `examples/bridge_real_agent.py` is the
framework-agent template.

## Reference

- `docs/design/` — the design specs; code comments reference these as "spec §N".
- `configs/profiles/` — example run profiles (system prompt + tool inventory + industry + depth).
- `examples/industries/` — per-vertical example configs.
