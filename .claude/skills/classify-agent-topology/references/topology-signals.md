# Topology & integration detection signals

Code-level markers that classify an agent without asking the user. Grep these; the strongest match wins.

## Topology

| Topology | Signals |
|---|---|
| `in_process_python` | `.py` files, an importable class/function entrypoint, NO web server bound. Best case: a **pure decision function** that returns a structured "what to do" object (dataclass/pydantic) with no IO inside. |
| `local_http` | `fastapi`/`flask`/`starlette`/`aiohttp.web`/`express` app, a route like `/v1/chat/completions` or `/chat`, `uvicorn`/`gunicorn` in deps or a `__main__` that serves. |
| `remote_hosted` | Only a deployed base URL / API docs; no runnable source, or source is a thin client to a SaaS. Managed-Agents / vendor endpoints. |
| `other_language` | `go.mod` / `package.json` (no Python) / `pom.xml` / `Cargo.toml`. In-process patch seams are NOT available from Python; fall to wire-stub or HTTP. |

## Integrations (`name:kind`)

| Kind | Signals |
|---|---|
| `:rest` | `httpx.AsyncClient(base_url=...)`, `requests.Session`, `aiohttp.ClientSession`; a `base_url`/host in config. |
| `:mcp` | imports `mcp` / `fastmcp`; `ClientSession`, `stdio_client`, `sse_client`, `session.call_tool(...)`. |
| `:rag` | `langchain`/`llamaindex` retrievers, `vectorstore`, `similarity_search`, `embed_query`, pinecone/weaviate/chroma/qdrant clients. |
| `:db` | `sqlalchemy`, `asyncpg`, `psycopg`, `motor`, raw SQL. |
| `:llm` | `anthropic`, `openai`, `google-genai`, a model-id config. **Never stub this** — it's the agent's brain; pass it through. |

## `surfaces_tool_calls`

- TRUE if the agent's output object/JSON already contains OpenAI-style `tool_calls` (or a list of
  chosen actions you can map 1:1). → HTTP shortcut viable.
- FALSE if tools execute internally and only a final text reply comes out. → instrumentation
  required at a waist (find-agent-seams), else tool-misuse is a blind spot.

## Framework entrypoints (where `run/invoke/chat` lives)

| Framework | Entrypoint hint |
|---|---|
| LangChain | `AgentExecutor.invoke` / `.ainvoke`, `Runnable.invoke`, tools via `@tool` / `StructuredTool`. |
| LlamaIndex | `agent.chat` / `.achat`, `FunctionTool`. |
| CrewAI | `Crew.kickoff`, `Agent`/`Task`. |
| AutoGen | `ConversableAgent.generate_reply` / `a_generate_reply`. |
| Semantic Kernel | `kernel.invoke`, `@kernel_function`. |
| custom | a class with `run`/`handle`/`chat`/`step`; trace where the LLM call and tool dispatch happen. |
