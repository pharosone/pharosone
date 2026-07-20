---
name: setup-dialog-streaming
description: Use when you need to wire an AI agent or bot codebase to stream its conversations — messages AND tool calls — into the PharosOne dialog-analysis cabinet: picks the Python/TypeScript/Go SDK or raw HTTP, pre-creates the agent with description and goal, and instruments per-turn send-message or snapshot send-dialog calls. Produces working integration code plus a verified test round-trip.
---

# Setup Dialog Streaming

Wire a bot codebase to the PharosOne dialog-analysis cabinet so every conversation it holds —
messages **and tool calls** — lands there for analysis. The integration is three small ingest
calls (`upsert-agent`, `send-message`, `send-dialog`); the real work is picking the right
transport for the codebase, instrumenting the bot loop without disturbing it, and proving one
round-trip end to end. Wire contract: `references/api.md`. Runnable code: `references/snippets.md`.

**Announce at start:** "Using setup-dialog-streaming to wire <bot> into the PharosOne cabinet."

## Checklist (todo per item)

1. Detect the target bot's language and framework (read the code — don't ask).
2. Choose the transport: the matching SDK (Python / TypeScript / Go) or raw HTTP
   (`references/snippets.md`).
3. Collect the base URL and the API-key **env-var NAME** — NEVER the secret value (below).
4. Upsert the agent with `description` + `goal` (`upsert-agent`).
5. Instrument the bot loop: per-turn `send-message` with `message_id` idempotency, including a
   `tool_call` entry for every tool invocation — or a `send-dialog` snapshot for batch/backfill.
6. Verify: one curl `send-message` round-trip, then confirm the dialog appears in the cabinet.

## 1. Detect language & framework

Read the codebase first; ask only what code can't answer. Identify the language (file
extensions, manifest: `pyproject.toml` / `package.json` / `go.mod`), the bot framework
(Telegram/Discord/Slack SDK, a web framework, an agent framework, a plain loop), and — most
important — the **turn boundary**: the one place where an incoming user message arrives, the one
place where the bot's reply leaves, and the place(s) where tools are invoked. Those are the
instrumentation points for step 5.

## 2. Choose SDK or raw HTTP

| Codebase | Transport |
|---|---|
| Python ≥3.10 | `pharosone-dialogs` (module `pharosone_dialogs`, class `PharosOne`) |
| TypeScript / Node (ESM) | `@pharosone/dialogs` (class `PharosOne`) |
| Go | `sdk/go` module, `pharosone.New(baseURL, apiKey)` |
| Anything else, or no-new-deps policy | raw HTTP (three POSTs, `curl`-shaped) |

All SDKs are zero-runtime-dependency and share the same three methods (`upsertAgent` /
`sendMessage` / `sendDialog`) with typed `ToolCall` and `Message`, and all fall back to
`PHAROSONE_BASE_URL` / `PHAROSONE_API_KEY` when constructor arguments are omitted. Constructors
are idiomatic per language — Python `PharosOne(base_url, api_key)`, TypeScript
`new PharosOne({ baseUrl, apiKey })` (single options object), Go `pharosone.New(baseURL, apiKey)`;
copy exact forms from `references/snippets.md`. Raw HTTP is always a valid choice — the whole
wire contract is three endpoints (`references/api.md`).

## 3. Base URL + key env var (never the secret)

Ask the user for:

- the **base URL** of their PharosOne instance (e.g. `https://pharos.example.com`);
- the **NAME of the environment variable** that holds their ingest API key
  (default suggestion: `PHAROSONE_API_KEY`).

The key itself is minted in the cabinet under **API Keys → New key** and is shown once, to the
user only. NEVER ask for, read, echo, or hard-code the secret value — the integration code reads
it from the env var at runtime, and every snippet you emit references the variable by name.

## 4. Upsert the agent

Pre-create the agent so dialogs attach to a named, described agent from the first message —
`description` and `goal` also feed the analyzer's context. `POST /api/v1/upsert-agent` is
idempotent: `agent_id` matches an existing agent by id or name, otherwise creates it; omitted
fields keep their stored values. Use a stable, human-readable `agent_id` (e.g. `support-bot`) and
fill `description` (what the bot is) and `goal` (what a successful conversation achieves) from
the codebase's own system prompt / README, confirmed with the user.

## 5. Instrument the bot loop

### Per-turn streaming vs snapshot — pick deliberately

- **Per-turn `send-message`** (default for live bots): call it at each turn boundary — one call
  per user message, per bot reply, and per tool invocation. The dialog appears in the cabinet
  live and analysis follows on idle. Always pass a stable `message_id` (e.g.
  `<platform-msg-id>` or `<session>-<seq>`): retries become upserts instead of duplicates, and a
  later patch (a tool result that arrives after the call was first reported) updates the same row.
- **`send-dialog` snapshot** (batch/backfill): one call replaces the whole dialog's message list.
  Use it for importing history, nightly exports, or bots whose framework only exposes a finished
  transcript. Re-sending the same `(agent_id, session_id)` replaces the previous snapshot — it is
  not an append.

Both paths key the dialog on `(agent_id, session_id)`; use the bot's own conversation/chat id as
`session_id` so a conversation maps to exactly one dialog.

### tool_call — send every tool invocation

Analysis sees tool calls only if you send them. For each tool invocation emit a message with
`role: "tool"` and a `tool_call` object; `text` may stay empty:

- `name` — the tool's exact identifier in code (not a paraphrase).
- `label` — short human-readable label for the cabinet transcript.
- `status` — `ok` (ran, returned normally) | `denied` (blocked by policy/permissions before
  running) | `error` (ran and failed) | `pending` (started, outcome not yet known — patch it
  later via the same `message_id`).
- `args_preview` / `result_preview` — short human-readable previews of the arguments and result,
  truncated (~200 chars is plenty). Previews, not payloads.

### What NOT to send

The cabinet stores what you send — trim at the source:

- **Secrets never**: API keys, tokens, passwords, connection strings — neither in `text` nor in
  `args_preview`/`result_preview`. If a tool takes credentials, redact them in the preview.
- **Trim PII to need**: send `end_user` fields the analysis actually benefits from; drop or mask
  the rest per the user's data policy.
- **Previews, not payloads**: don't dump full documents, raw HTML, or base64 blobs into
  `result_preview` — a one-line summary is what the analyst (and the analyzer) needs.

## 6. Verify the round-trip

Prove the wiring with the real key from env before touching the bot's runtime (full commands in
`references/snippets.md`):

1. `curl` one `send-message` with a test `session_id` — expect HTTP 202 and a body with
   `"status": "received"`, a `dialog_id`, `message_index`, `created: true`.
2. Ask the user to open the cabinet's **Dialogs** page and confirm the test dialog shows the
   message (and the tool call row, if you sent one).
3. Then run the instrumented bot for one real turn and confirm that dialog appears too.

If step 1 returns 401, the env var doesn't hold a valid ingest key; 422 means the request body
violates the contract — compare against `references/api.md`.
