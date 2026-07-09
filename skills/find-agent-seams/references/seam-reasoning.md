# Seam reasoning angles (interception, not exploitation)

A reusable checklist for finding the **narrowest reachable waist** in an agent's code. Classic
red-team methodology asks "how do I *exploit* this?"; onboarding flips the question to "where do I
*intercept* this?" — the one point above transport and below reasoning where every tool call is
observable, every untrusted channel is injectable, and every side effect is neutralizable.

Run these angles over the repo (grep + read). Each angle gives a **signal** (what to look for) and
what it **implies** (the technique + injectable channel it usually points to). More than one angle
often lands on the same waist — that convergence is the recommended seam. A needed channel that no
angle can reach is a **blind spot**: surface it, never let it read as robust.

Techniques: `param_inject` (A) · `monkeypatch` (B) · `dep_mock` (C) · `wire_stub` (D) · `live_seed` (E).
Channels: `message` · `history` · `card_field:<name>` · `tool_result:<tool>` · `retrieved_doc` ·
`memory` · `file_content` · `image_content`.

---

## The angles

1. **Where is agent/session state cached?**
   - *Signal:* a module-level dict, a `Session`/`Conversation` object, an on-disk/Redis store keyed
     by session id; anything that persists across turns.
   - *Implies:* the seed point for `history` / `memory` channels. If state is a plain param to a
     decision fn → A; if it's read through a client/store object → C or B on the read path.

2. **Where is the LLM client constructed — and is it cached at import?**
   - *Signal:* `OpenAI(...)`, `Anthropic(...)`, `get_model(...)`, a module-level singleton or a
     `@lru_cache`d factory. Note WHEN it runs (import time vs first call).
   - *Implies:* the brain stays real (never a seam), but the construction *timing* dictates ordering:
     any `monkeypatch` (B) on tools/MCP must be applied **before** this client is built and bound.

3. **Is `base_url` actually overridable, or is there TLS pinning?**
   - *Signal:* `httpx.AsyncClient(base_url=...)`, `requests.Session`, MCP transport URL. Is the URL a
     literal or read from env/config? Any `verify=`, cert pinning, or mTLS?
   - *Implies:* only a repointable, unpinned URL supports `wire_stub` (D). A hardcoded/pinned URL
     rules D out — go up to a `monkeypatch` (B) waist above transport instead.

4. **Does the agent re-verify a `tool_result` before acting on it?**
   - *Signal:* the code path from a tool's return value to the next decision — is the result trusted
     verbatim, or re-checked/re-fetched?
   - *Implies:* the trust boundary is exactly where poison enters via `tool_result:<tool>`. Patch the
     dispatch/return path (B) to inject there; unverified results are the highest-value channel.

5. **What is the dispatcher's sad-path (unknown tool / error branch)?**
   - *Signal:* the `else` / `except` / "tool not found" arm of the tool router; default returns,
     silent swallows, retries.
   - *Implies:* the single `dispatch(name, args)` waist to `monkeypatch` (B) — it mediates ALL tools,
     including the ones the happy path forgets. One patch covers every tool + backend.

6. **Where does untrusted content first enter the process?**
   - *Signal:* ingestion surfaces — a CRM/record fetch, a retrieved doc, an inbound message field, a
     file read, an image blob, a memory read.
   - *Implies:* each entry point is a channel to declare: `card_field:<name>` / `retrieved_doc` /
     `message` / `file_content` / `image_content` / `memory`. The narrowest common reader is the seam
     (often the retrieval or DI read path → B or C).

7. **What is the narrowest point ABOVE transport but BELOW reasoning?**
   - *Signal:* the funnel where N servers × M tools × any auth collapse into one function
     (`call_tool`, `execute_tool`, `retriever.get`).
   - *Implies:* the canonical `monkeypatch` (B) target. Cutting here covers many downstream concerns
     while keeping the model real. Prefer this over cutting at the wire (D) or the brain.

8. **Is there a pure decision function separated from its `apply`/`commit`?**
   - *Signal:* a function returning a structured "what to do" object (dataclass/pydantic/TypedDict)
     with **no** IO in its body, plus a sibling `apply`/`execute`/`_apply_turn` that performs effects.
   - *Implies:* the cheapest seam — `param_inject` (A). Pass poison as a param, read the returned
     actions as the ledger; channels `message` / `card_field:*` / `history`. No patch needed.

9. **What survives serialization round-trips?**
   - *Signal:* JSON/pickle/proto encode–decode between components; fields dropped, coerced, or renamed
     across a boundary; unicode/escaping normalization.
   - *Implies:* inject ABOVE the round-trip so the payload the agent reasons over is the payload you
     sent. If a channel's content doesn't survive the trip, that channel is a blind spot at this seam.

10. **Where do two parsers disagree?**
    - *Signal:* the same bytes parsed twice (e.g. a lenient loader for ingestion, a strict one for
      the guard), differing content-type handling, markdown vs plaintext readers.
    - *Implies:* the injection point that reaches the *reasoning* parser, not just the guard parser —
      otherwise the poison is filtered before it matters. Choose the seam on the reasoning side.

11. **Where is the tool registry / capability table built?**
    - *Signal:* `TOOLS = {...}`, a `@tool`/`@register` decorator registry, an OpenAI `tools=[...]`
      schema, `self._tools[name] = fn`.
    - *Implies:* confirms the `monkeypatch` (B) waist AND gives the EXACT tool names the shim must
      surface — mismatched names mean the oracle silently never fires.

12. **Where do side effects actually commit?**
    - *Signal:* the last mile before an irreversible action — `send`, `transfer`, `delete`,
      `set_status`, `write_memory`, a deploy/ship call.
    - *Implies:* the neutralization point. Whatever seam you pick, this call must be recorded with a
      benign return, never performed. If it can't be intercepted at any reachable seam, tool-misuse
      coverage is a blind spot (only the text oracles work).

---

## Using the angles

- Sweep all 12; note which seam each converges on. The **narrowest** one that is reachable given the
  passport (`topology`, `source_modifiable`) becomes `recommended: true`.
- For every angle that reaches an ingestion surface, add its channel to the seam's `channels[]`.
- For every needed channel no angle reaches, record a `blind_spot` — a skipped vector must read as a
  gap, never as a pass.
- Cross-check detect patterns and the narrowness rubric in `waist-detectors.md`; the decision tree
  that maps waist → technique is the canonical one in `../../pharosone/SEAM_PIPELINE.md` → **Decision tree**.
