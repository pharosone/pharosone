# Onboarding Skill Suite — implementation-ready design

Decisions (locked): **5 sub-skills + router**; **`onboard-agent` evolves into the router, renamed `pharosone`**.
Companion: `SEAM_PIPELINE.md` (the channel contract + waist rule + shim templates).

Authoring canon applied: 1 skill = 1 capability · `description` = "Use when…" + triggers (routing
surface) · progressive disclosure (lean SKILL.md, detail in references) · checklist→todos ·
announce-at-start · artifacts passed as FILES under `harness/<agent>/` (durable, subagent-visible).

---

## Shared artifact schemas (the contracts between skills)

**Single source of truth = the JSON schemas, not this prose.** The canonical structure of both
machine blocks now lives in machine-readable, `additionalProperties:false` schemas, and a
zero-dependency validator checks any artifact against them (mechanical structure) plus a few
cross-field invariants (semantics) that a plain schema can't express. The validator + schemas now
live in the `probe_engine.onboarding` package (shipped with the engine, not the skill) and are
invoked via the `probe-engine validate-artifacts` CLI command:

- `probe_engine/onboarding/schemas/passport.schema.json` — passport structure (topology grammar,
  integration `name:kind` grammar, the canonical-capability enum from
  `build-run-profile/references/capability-vocabulary.md`, the canonical-channel enum from
  `CANONICAL_CHANNELS` in `src/probe_engine/domain/probe.py`).
- `probe_engine/onboarding/schemas/seams.schema.json` — seams structure (the `technique` enum,
  `narrowness`, the seam-channel grammar incl. parameterized forms like `tool_result:*` /
  `card_field:<name>`).
- `probe_engine.onboarding.validate` — the validator (stdlib only). It reads the schemas at
  runtime for the **mechanical** checks (types, enums, `additionalProperties:false`, string
  patterns) and hand-codes the **semantic** invariants: passport `channels`/`blind_spots` must be
  disjoint; seams must have exactly one `recommended:true`, every `narrowness` in `1..5`, and the
  recommended seam must declare ≥1 channel. Mechanical and semantic checks are kept separate on
  purpose. Run it on a `.json` file OR a `.md` with an embedded ```json block:

  ```bash
  probe-engine validate-artifacts passport harness/<agent>/PASSPORT.md
  probe-engine validate-artifacts seams    harness/<agent>/SEAMS.md
  ```

`harness/<agent>/PASSPORT.md` (+ a machine block `passport.json`) — illustrative (abridged) shape;
the schema is authoritative and real artifacts also carry `agent`, `framework`, `channels`,
`blind_spots` (and the optional `model_config`, `tool_dispatch_waist`, `system_prompt_variants`,
`defenses`):
```json
{ "topology": "in_process_python | local_http | remote_hosted | other_language",
  "language": "python", "entrypoint": "app.llm:QualifierLLM.run_turn",
  "surfaces_tool_calls": false, "source_modifiable": true, "live_backend_ok": false,
  "integrations": ["crm:rest", "messaging:rest", "anthropic:llm"],
  "tools": [{"name":"reply_to_client","capabilities":["send_message"],"dangerous":true}, ...],
  "system_prompt_path": "prompts/system_prompt.md" }
```

`harness/<agent>/SEAMS.md` (+ `seams.json`) — the embedded machine block is a **bare array** of
seam objects (`blind_spots` lives in the surrounding markdown prose, not in the array):
```json
[{ "seam":"decision_fn", "file":"app/llm.py:531", "narrowness":1,
   "technique":"param_inject", "channels":["message","card_field:lead_snapshot","card_field:contact_name"],
   "recommended":true },
 { "seam":"io_client", "file":"app/crm.py:11", "narrowness":4,
   "technique":"wire_stub", "channels":["card_field:*","tool_result:*"], "recommended":false }]
```
Plus `blind_spots: ["tool_result:* — agent reads card via code, no tool channel"]`.

`harness/<agent>/adapter.py` — implements the universal contract: `external(request)` +
`channels()` + `injection` routing (per `SEAM_PIPELINE.md` §0).

`configs/profiles/<agent>.yaml` — as today (tools→capabilities, protected_snippets, thresholds).

---

## 1. pharosone  (ROUTER — thin composite)

`description`: *Use when the user points at a folder containing a real AI agent to test/certify
with the Probe Engine. Orchestrates the full pipeline — classify topology, find the interception
seam, generate the bridge adapter, build the run profile, validate, and hand off a ready-to-run
certification.*

Announce: "I'm using pharosone to wire <path> for certification."

Checklist (todo per item):
1. Invoke **classify-agent-topology** → `PASSPORT.md`.
2. Invoke **find-agent-seams** (DISPATCH SUBAGENT) → `SEAMS.md`. Pick the recommended seam (or ask
   the user if top-2 are close / a high-value channel is only reachable by a costlier seam).
3. Invoke **generate-agent-shim** with the chosen seam → `adapter.py`.
4. Invoke **build-run-profile** → `profile.yaml`.
5. Invoke **validate-and-certify** → alignment + smoke + handoff command.

Body = the §"Decision tree" from `SEAM_PIPELINE.md` + delegation. Keeps NO stage logic inline.

---

## 2. classify-agent-topology   [inline]

`description`: *Use when you need to determine HOW an agent is reachable — in-process Python,
local HTTP server, remote/hosted, or another language — and which integrations it has, to pick a
seam family before wiring it.*

Detects: extensions/entrypoint; web server (`fastapi`/`flask`); MCP libs (`mcp`,`fastmcp`);
framework (`langchain`/`llamaindex`/`crewai`/`autogen`); IO clients; whether output carries
`tool_calls`. Asks only what code can't answer (source modifiable? live backend ok?).
Output: `PASSPORT.md` (+ `passport.json`). Reference: `references/topology-signals.md`.

---

## 3. find-agent-seams   [DISPATCH SUBAGENT — read-heavy fan-out]

`description`: *Use when you need to locate the narrowest interception waist in an agent's code —
the pure decision function, the tool-dispatch registry, an MCP `call_tool`, a retrieval client, a
DI constructor, or the raw IO client — and rank candidates for wiring a test harness.*

Dispatch an Explore-style subagent: "find dispatch chokepoints in <repo>, return ranked JSON per
the SEAMS schema — file:line, narrowness, technique, injectable channels; flag blind spots." The
subagent returns ONLY the structured list (no file dumps → main context stays clean).
Narrowness rule: above abstractions (auth/transport/serialization), below reasoning.
Output: `SEAMS.md` (+ `seams.json`). Reference: `references/waist-detectors.md` (per-framework
detect patterns + the narrowness scoring rubric).

---

## 4. generate-agent-shim   [SUBAGENT optional — fan-out one per candidate seam to compare]

`description`: *Use when you have a chosen seam and need a bridge adapter implementing the channel
contract — selecting param-inject / monkeypatch / dep-mock / wire-stub / live-seed and filling it
from recon.*

Picks the template by `technique`, fills from PASSPORT+SEAMS, emits `adapter.py` with `external()`
+ `channels()` + injection routing. If seams are close, fan out a subagent per candidate (each
emits a draft), then the router picks. MUST neutralize side effects; MUST surface `tool_calls`.
Templates: `templates/{A_param_inject.py, B_monkeypatch.py, C_dep_mock.py, D_wire_stub.py,
E_live_seed.py}`.

---

## 5. build-run-profile   [inline]

`description`: *Use when you need a Probe Engine run profile for an onboarded agent — tools mapped
to canonical capabilities, protected_snippets for prompt_leak, attacker/paraphrase/judge models,
and certification depth/thresholds.*

= the profile half of today's pharosone. Output: `configs/profiles/<agent>.yaml`. Reuses
`templates/profile_template.yaml`. Reference: `references/capability-vocabulary.md`.

---

## 6. validate-and-certify   [inline]

`description`: *Use when an agent has an adapter + profile and you need to validate
capability alignment, run a 1-trial smoke, surface blind spots, and hand off the ready-to-run
certification command.*

Steps: `probe-engine validate`; capability alignment (probe `required_tools` ⊆ inventory caps);
confirm `channels()` covers the corpus's indirect vectors (else report blind spots); 1-trial
smoke (HTTP or `certify(n=1)`); print the certification command + how to read pass/fail.

---

## File layout to create
```
skills/
  pharosone/
    SKILL.md                      (rewrite → router)
    SEAM_PIPELINE.md  PIPELINE_DESIGN.md   (exist)
    templates/{profile_template.yaml, adapter_template.py(legacy)}
  classify-agent-topology/SKILL.md
    references/topology-signals.md
  find-agent-seams/SKILL.md
    references/waist-detectors.md
  generate-agent-shim/SKILL.md
    templates/{A_param_inject.py,B_monkeypatch.py,C_dep_mock.py,D_wire_stub.py,E_live_seed.py}
  build-run-profile/SKILL.md
    references/capability-vocabulary.md
  validate-and-certify/SKILL.md
```

## Reuse note
The `example-agent` adapter == template **A** (param-inject) with `channels()` not yet
declared. Backfilling `channels()` + injection routing into it is the first validation of the
design (and closes the card-field blind spot).
```
