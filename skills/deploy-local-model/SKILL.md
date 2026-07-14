---
name: deploy-local-model
description: Use when the pharosone onboarding's LLM-mode question picks "Local" and a self-hosted attacker/judge model needs to actually exist — resolves a model choice (Granite recommended, or a custom Hugging Face repo) onto either an endpoint the user already has running, or a freshly installed local/remote server (llama.cpp GGUF on CPU, or vLLM on GPU) — and returns the Inspect AI model string(s) pharosone's run profile needs. Never requires a real API key: self-hosted servers take a dummy placeholder, never a secret.
---

# Deploy Local Model

> **Authorized defensive use.** Same authorization scope as the rest of the PharosOne skills — this
> only ever installs/serves a model to drive certification of an agent the operator is authorized to
> test. It starts a local (or operator-named remote) inference server; it never sends anything to a
> third party the operator didn't choose — a fully local server is the entire point of this branch.

Sub-skill invoked from `pharosone`'s Round B (`0.4.1`) when the operator picks **Local** for the LLM
mode. Turns a model choice + serving preference into a running (or already-running) OpenAI-compatible
endpoint, and hands back the exact Inspect AI model string(s) + env vars for
`configs/profiles/<agent>.yaml`.

**Announce at start:** "Setting up the local model — `<model>` on `<cpu/gpu>`, `<local/remote>`."

## Inputs (already collected by pharosone 0.4.1 — don't re-ask)

- `model` — a Hugging Face repo. Default recommendation: the current Granite instruct model (e.g.
  `ibm-granite/granite-3.3-8b-instruct` — **check Hugging Face for a newer Granite instruct tag before
  pulling**; IBM ships new Granite generations periodically and this skill should always resolve to
  the latest one unless the operator names a specific version). `Other` is any HF repo the operator
  typed.
- `serving` — `cpu` (GGUF via `llama.cpp`) or `gpu` (vLLM).
- `location` — `this machine` or `a remote server` (host the operator named).
- `install` — `already running, give me the endpoint` (skip straight to health-check + record
  `base_url`) or `set it up for me` (do the install below).

## CPU path — GGUF via llama.cpp (`serving: cpu`, `install: set it up for me`)

1. Locate (or download) a GGUF quantization of `model` — prefer an official or well-known verified
   quant. `Q4_K_M` is the default speed/quality balance; go to `Q5_K_M`/`Q6_K` only if the operator
   wants higher fidelity and has the RAM headroom (roughly the quant size in GB, plus room for
   context).
2. Install `llama.cpp`'s server (`llama-server`) if not already on PATH — a release binary for the
   host OS/arch, or build from source. (`pip install llama-cpp-python` is the alternative if a Python
   dependency is preferred to a standalone binary; either exposes an OpenAI-compatible `/v1` route.)
3. Launch: `llama-server -m <gguf-path> --host 127.0.0.1 --port 8000 -c 8192` (context sized to the
   corpus' longest chain/adaptive transcripts — 8192 is a safe floor).
4. Health-check `curl -s localhost:8000/v1/models` returns the loaded model before reporting success.
5. Model string: `openai-api/pharos-local/<model-name>`; env vars
   `PHAROS_LOCAL_BASE_URL=http://127.0.0.1:8000/v1` and `PHAROS_LOCAL_API_KEY=local` (a placeholder —
   `llama-server` doesn't validate it, so **this is not a secret**; it's fine to set it inline rather
   than asking the operator to `export` anything).

**State this tradeoff before starting:** works on any machine, no GPU required, nothing leaves the
host — but throughput is low (expect single-digit tokens/sec for an 8B model on a laptop CPU). Fine
for a `≤10%`/`≤5%` screening depth; a real bottleneck for a `≤2%`/`≤1%` deep run's judge volume. Say
this plainly so the operator's depth expectations (set back in Round A) are still realistic.

## GPU path — vLLM (`serving: gpu`, `install: set it up for me`)

1. Confirm a usable GPU first (`nvidia-smi` — note total VRAM). An 8B instruct model needs roughly
   16–20GB VRAM at fp16/bf16, or ~8–10GB with an AWQ/GPTQ quantized checkpoint for smaller cards. If
   VRAM is short, say so and offer to fall back to the CPU/GGUF path instead of failing silently.
2. `pip install vllm` (or a pinned container image if the host prefers isolation).
3. Launch: `vllm serve <model> --host 127.0.0.1 --port 8000` (add `--quantization awq`/`gptq` if
   serving a quantized checkpoint).
4. Health-check the same `/v1/models` route before reporting success.
5. Model string: `vllm/<model>`; env var `VLLM_BASE_URL=http://127.0.0.1:8000/v1` (vLLM's own
   convention — no separate PharosOne var needed). No real key either — `VLLM_API_KEY` defaults to a
   placeholder if unset.

**State this tradeoff:** far higher throughput than CPU/GGUF (viable for `≤2%`/`≤1%` deep runs), but
needs a real GPU with enough VRAM, and the install is heavier (CUDA toolchain, a larger download).
Recommend GPU whenever the host has one and the chosen depth is past `≤5%`.

## Remote server (`location: a remote server`)

Same two paths, just executed over the named remote host. **Never ask the operator to paste an SSH
password or private key into chat** — ask only for the host, and confirm they already have working
key-based/agent-forwarded SSH access (the same "env-var name / access confirmation only, never the
secret" rule used everywhere else in this skill family). If a read-only connectivity check
(`ssh <host> true`) fails, stop and report exactly what's missing — do not attempt to prompt for or
handle a password interactively. Once installed, `base_url` points at
`http://<remote-host>:<port>/v1`; the operator owns making that port reachable from wherever the
certification actually runs (their own firewall/VPN/tunnel choice — this skill doesn't open one).

## Already-running endpoint (`install: already running, give me the endpoint`)

No install. Ask, in prose: the `base_url` (e.g. `http://10.0.0.4:8000/v1`) and which server it is
(vLLM / llama.cpp / another OpenAI-compatible server), so the right Inspect provider prefix and env
var get used. Health-check `<base_url>/models` before accepting it — a wrong port/host should fail
loud here, not three steps later during the actual certification run.

## Judge model, today vs soon

The engine's judge prompt (`src/probe_engine/scoring/judge.py`'s `_TEMPLATE`) is already a
purpose-written "strict red-team judge" instruction — pointing `judge_model` at the same local model
just stood up gets a real semantic judge for free, no second deployment needed (reuse the attacker
deployment unless the operator wants two separate ones). **A PharosOne-tuned judge model, fine-tuned
specifically for this adjudication task, is coming to Hugging Face soon** — when it ships, swapping
`judge_model` to it is a one-line profile change, nothing else in this flow changes. Say this to the
operator so they know today's local judge is "Granite + a strong prompt," not a permanent ceiling.

## Output back to pharosone

Report: the resolved Inspect model string(s) (one for `attacker_model`/`paraphrase_model`, one for
`judge_model` — same string by default, reusing one deployment for both), the `base_url`, and an
explicit confirmation that **no real secret was ever requested** — a placeholder key was set only
because the client library requires *some* non-empty string, never because anything is actually
gated on it.
