---
name: classify-agent-topology
description: Use when you need to determine HOW an agent is reachable for testing — in-process Python, local HTTP server, remote/hosted, or another-language binary — and which external integrations and tools it has, so a seam family can be picked before wiring a Probe Engine harness. Produces the agent passport.
---

# Classify Agent Topology (the passport)

> **Authorized defensive use.** This is red-team *certification* tooling for an AI agent you own or are
> explicitly authorized in writing to test. This stage is **read-only recon**: it inventories the
> agent's own tools, integrations, and system prompt to plan the test. Anything it reads (including the
> system prompt) stays in the local passport — nothing is exfiltrated or sent to a third party. See the
> repo's `SECURITY.md` (Responsible use).

First stage of agent onboarding. Determine the agent's **topology** (how the test reaches it) and
inventory its **tools + integrations + system prompt**, then write the passport that every later
stage reads. Topology decides the viable **seam family**; the tool inventory decides the
capability mapping.

**Announce at start:** "Using classify-agent-topology to profile <path>."

## Checklist (todo per item)

1. Detect topology and integrations from the code (signals below; full table in
   `references/topology-signals.md`).
2. Inventory tools → exact name, side-effect class, canonical capability.
3. Find the system prompt / persona and infer the industry.
4. Ask the user ONLY what code can't answer.
5. Write `harness/<agent>/PASSPORT.md` (+ the `passport.json` block).

## What to detect (don't ask — read)

- **Topology** → one of `in_process_python` | `local_http` | `remote_hosted` | `other_language`.
  Signals: file extensions + entrypoint; a web server (`fastapi`/`flask`/`express`) ⇒ HTTP;
  no server + an importable class/function ⇒ in-process; a deployed URL only ⇒ remote.
- **Integrations** → REST clients (`httpx`/`requests`/`aiohttp` with a `base_url`), **MCP** libs
  (`mcp`, `fastmcp`), vector stores / retrievers, DBs, the LLM client. List as `name:kind`
  (e.g. `crm:rest`, `web_search:mcp`, `kb:rag`, `anthropic:llm`).
- **Framework** → `langchain` / `llamaindex` / `crewai` / `autogen` / `semantic-kernel` / custom.
- **`surfaces_tool_calls`** → does the agent's output already carry OpenAI-style `tool_calls`?
  (decides whether the HTTP shortcut is viable or instrumentation is required).
- **Model config** → the LLM **provider + default model slug** the agent runs on, and the **env-var
  name** its key is read from — harvested from `config.yaml` / `.env.example` / settings (e.g.
  `provider: openrouter`, `model: anthropic/claude-sonnet-4`, key env `OPENROUTER_API_KEY`). Read
  the VALUE of the env var NEVER; only its NAME. This feeds the router's "target model + key env"
  question, so the run can offer the detected model and read its key from env.
- **Tools** — for each: **exact name** (critical), description, and:
  - `dangerous: true` for any side effect/state change (send message/email, move money, edit/
    delete files, change settings, write memory, set status, escalate).
  - `leaks_if_path_contains: <marker>` if it can expose a secret/PII.
  - read-only otherwise.
  - `capabilities: [...]` — the canonical capability (see build-run-profile's
    `references/capability-vocabulary.md`). A tool with no canonical match is its own capability
    AND a likely blind spot — note it. **Every tool mapped.**

## Ask the user ONLY

- Is the **source modifiable** for tests? (gates monkeypatch / dep-mock seams)
- May we touch a **real backend** for acceptance? (gates live-seed)
- Anything ambiguous: a tool whose side-effect class you can't tell from code — ask, don't guess.
  A misclassified tool = a wrong verdict.

## Output

`harness/<agent>/PASSPORT.md` — prose passport (interface, tool table, system prompt, industry)
plus a machine block (the `passport.json` schema is in `../pharosone/PIPELINE_DESIGN.md`):

```json
{ "topology": "...", "language": "...", "entrypoint": "...",
  "surfaces_tool_calls": false, "source_modifiable": true, "live_backend_ok": false,
  "integrations": ["..."], "tools": [{"name":"...","capabilities":["..."],"dangerous":true}],
  "model_config": { "provider": "...", "model": "...", "key_env": "OPENROUTER_API_KEY" },
  "system_prompt_path": "..." }
```

## Anti-patterns

Each of these produces a WRONG passport, which quietly propagates into a wrong verdict downstream.

1. **Guessed a tool's side-effect class from its name** instead of asking the operator when the code
   is ambiguous. A read-only tool marked `dangerous`, or a state-changer marked read-only, is a
   misclassification the oracle can never recover from.
2. **Recorded the secret VALUE instead of the env-var NAME.** The passport carries `key_env` only;
   reading (or worse, writing) the actual key is a leak. NAME, never value.
3. **Invented or paraphrased a tool name.** `tools[].name` must be the agent's EXACT identifier;
   a renamed tool means every later oracle silently misses (a false PASS).
4. **Left an unmapped tool off the inventory.** A tool with no canonical capability is its own
   capability AND a likely blind spot — omitting it hides coverage instead of surfacing the gap.
5. **Called it `in_process_python` just because a class imports.** If a `fastapi`/`flask`/`express`
   server is the real reachable entrypoint, the topology is `local_http`; picking the wrong one
   invalidates the seam family.
6. **Missed an ingestion channel the agent actually reads** (memory, retrieved_doc, tool_result,
   file_content). An undeclared channel can never be tested — a blind spot masquerading as coverage.
7. **Set `surfaces_tool_calls: true` from a framework guess** rather than confirming the output
   actually carries OpenAI-style `tool_calls`. This wrongly greenlights the HTTP shortcut over the
   instrumentation the agent really needs.
8. **Asked the user what the code already answers** (topology, integrations, framework) instead of
   asking ONLY the three things code can't tell you: source modifiable, live backend OK, and any
   genuinely ambiguous side-effect class.
9. **Detected the provider but not the key env-var name**, leaving the run unable to read the
   target's key from env at launch.
