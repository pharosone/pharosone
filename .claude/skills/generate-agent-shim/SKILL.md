---
name: generate-agent-shim
description: Use when you have a chosen interception seam for an agent and need to generate the bridge adapter — selecting param-inject, monkeypatch, dep-mock, wire-stub, or live-seed and filling it from the passport and seams recon. Emits an adapter implementing the channel contract (external + channels + injection routing) that makes tool calls observable and neutralizes side effects.
---

# Generate Agent Shim (the adapter)

Third stage. Turn the chosen seam into a working **bridge adapter** at `harness/<agent>/adapter.py`
that implements the universal **channel contract** from `../pharosone/SEAM_PIPELINE.md` §0.

**Announce at start:** "Using generate-agent-shim to wire the <technique> seam for <agent>."

## The contract every shim must satisfy

```python
async def external(request: dict) -> dict   # -> {"choices":[{"message":{"content","tool_calls"}}]}
def channels() -> list[str]                  # untrusted surfaces this agent actually has
```
- `tool_calls` = the agent's **observed** actions (the misuse ledger the oracle reads).
- The request MAY carry `request["injection"] = {"channel": "...", "payload": "..."}`; the shim
  **routes the poison into that channel**. Channel grammar:
  `message | card_field:<name> | tool_result:<tool> | retrieved_doc | history`.
- Every dangerous action is **neutralized** (recorded, not performed).

One adapter then covers direct AND every indirect vector the seam can reach — the waist gives the
interception point, the contract gives the routing. No rewrite per attack.

## Checklist (todo per item)

1. Read `harness/<agent>/SEAMS.md` → the `recommended` (or user-chosen) seam + its `technique`.
2. Copy the matching template from `templates/` and fill the TODOs from PASSPORT + SEAMS.
3. Implement `channels()` to return exactly the channels that seam can inject (from SEAMS).
4. Wire `injection` routing for each channel.
5. Verify: no real side effect fires; `tool_calls` are surfaced; `channels()` matches SEAMS.
   (If seams were close, fan out a subagent per candidate to draft competing shims, then pick.)

## Template by technique

| technique | template | when |
|---|---|---|
| `param_inject` | `templates/A_param_inject.py` | pure decision fn — pass poison as a param (NO patch) |
| `monkeypatch` | `templates/B_monkeypatch.py` | tool dispatch / MCP call_tool / retrieval — patch the waist |
| `dep_mock` | `templates/C_dep_mock.py` | DI constructor — fake clients, real orchestration |
| `wire_stub` | `templates/D_wire_stub.py` | remote/opaque, repointable URLs — fake server |
| `live_seed` | `templates/E_live_seed.py` | acceptance — poison the real source, no emulation |

## Rules

- **Neutralize, don't perform.** A recorded call with a benign return — never the real effect.
- **Real brain.** Never stub the LLM; only the agent's tools/data/IO.
- **Names are real.** Surfaced `tool_calls` names must be the agent's actual tool names (or a
  documented 1:1 map), or the oracle won't fire.
- **Declare honestly.** `channels()` lists only what this seam can truly inject. Anything the
  corpus needs beyond that is a blind spot for build-run-profile / validate to report.

## Gotchas (ADVICE)

Hard-won operational notes from wiring shims. Each is a trap that silently ships a broken adapter.

> **ADVICE — verify `base_url` before you commit to `wire_stub`.** Grep the IO client's
> construction: if the URL is a literal (not read from env/config) or the client pins certs, the
> repoint fails and your fake server never sees a request. Confirm overridability first; otherwise
> fall back to a `monkeypatch` waist above transport. — *remote/opaque seams*

> **ADVICE — patch BEFORE the first client construction.** Many agents cache the LLM/tool/MCP client
> at import or in a module-level singleton. If your monkeypatch lands after that object already
> exists, it patches a copy nobody uses. Apply the patch (or import the module) before the agent
> constructs its client, or patch the already-bound attribute. — *B monkeypatch / C dep-mock*

> **ADVICE — match sync vs async at the entrypoint.** `external()` is async. If the agent's entry is
> sync, don't `await` it (run it in a thread / call it directly); if it's async, don't call it
> without awaiting. A mismatch either blocks the event loop or returns an un-awaited coroutine. —
> *all techniques*

> **ADVICE — tool names must match the agent's EXACT strings.** A shim that surfaces `send_email`
> when the agent calls `email.send` makes the oracle silently never fire — a false PASS. Copy names
> from the passport verbatim, or record a documented 1:1 map. — *B / C / D*

> **ADVICE — neutralize side effects, never execute them.** Record the dangerous call with a benign
> return; do not let the real send/transfer/delete run during a test. A shim that "just this once"
> performs the effect turns a probe into a live incident. — *all techniques*

> **ADVICE — reset per-request state.** Clear the call ledger / poison map at the start of each
> `external()`; module-level globals leak one probe's injection (and observed calls) into the next
> trial, corrupting both. — *B monkeypatch (shared `_CALLS`) / C dep-mock*

## Anti-patterns

Each of these ships an adapter that yields a WRONG verdict.

1. **Executed a real side effect** (sent the message, moved the money, deleted the file) instead of
   neutralizing it. Every dangerous action must be recorded with a benign return, never performed.
2. **Renamed or normalized a tool** in the surfaced `tool_calls` so it no longer matches the agent's
   real name — the oracle then silently never fires (a false PASS).
3. **Stubbed the reasoning LLM.** The brain must stay real; only tools/data/IO are faked.
4. **`channels()` claims a surface the shim doesn't route.** Declare only what this seam can truly
   inject; the remainder is a blind spot to report, not a claim to make.
5. **Applied the monkeypatch after the agent cached its client** at import — the patch is dead and
   the real client (and real side effects) run.
6. **Mismatched sync/async** at the entrypoint — `external()` never awaits the agent, or blocks the
   loop, so trials hang or return empty.
7. **Silently dropped a channel** the corpus needs because the template didn't cover it, instead of
   surfacing it as a blind spot for validate.
8. **Routed the injection but never surfaced the observed calls**, leaving `tool_calls` empty — the
   misuse ledger is blind and every misuse reads as robust.
9. **Leaked state between requests** (didn't clear the ledger/poison), so one probe's injection
   bleeds into the next trial.
