"""Generated bridge adapter for <AGENT> — wraps the real agent for PharosOne certification.

It (1) OVERRIDES the agent's tools with recording stubs so every call is captured and its side
effect NEUTRALIZED, (2) exposes `external(request)->response` (OpenAI shape) that the engine
drives, surfacing the recorded tool calls so the oracle can detect "called a tool it shouldn't",
and (3) `certify()` runs the corpus and writes a report. Fill every TODO from your recon.

Run:  uv run python harness/<AGENT>/adapter.py
"""

import datetime
import json
import pathlib
import uuid
from contextvars import ContextVar

# --- 1. import the real agent (TODO: point at the real entrypoint) -------------------------
# from your_agent.app import build_agent
# AGENT = build_agent()

# per-turn buffer of tool calls the agent made (contextvar = safe under async/concurrency)
_CALLS: ContextVar[list | None] = ContextVar("_CALLS", default=None)


def _as_json(a) -> str:
    return a if isinstance(a, str) else json.dumps(a, default=str)


def _record(name: str, arguments) -> None:
    buf = _CALLS.get()
    if buf is not None:
        buf.append({"type": "function", "function": {"name": name, "arguments": _as_json(arguments)}})


# --- 2. OVERRIDE the agent's tools: record + neutralize (TODO: wire to the real dispatch) ---
def _instrument() -> None:
    """Replace each real tool impl with a recording stub that does NOT perform the side effect.
    The exact patch point depends on the framework — pick the one that matches the agent:

    # (a) dict/registry of callables:
    # import your_agent.tools as T
    # for name in list(T.REGISTRY):
    #     def stub(*args, _n=name, **kw):
    #         _record(_n, kw or list(args))
    #         return f"[sandbox] {_n} not executed"
    #     T.REGISTRY[name] = stub

    # (b) framework tool objects (LangChain/CrewAI): wrap each tool's run/_run/func.
    # (c) MCP / HTTP tool backend: stand up stub endpoints that _record() and return canned data.
    """
    raise NotImplementedError("TODO: instrument the agent's tools (see options above)")


_instrument()


# --- 3. the bridge callable the engine drives ---------------------------------------------
async def external(request: dict) -> dict:
    buf: list = []
    token = _CALLS.set(buf)
    try:
        user_msg = request["messages"][-1]["content"]
        # TODO: run the real agent on user_msg (use full history if the agent expects it):
        # result_text = await AGENT.arun(user_msg)
        result_text = ""  # <- replace with the agent's textual reply
    finally:
        _CALLS.reset(token)
    return {"choices": [{"message": {"role": "assistant", "content": result_text, "tool_calls": buf}}]}


# --- 4. run certification (drives the engine with this adapter) -----------------------------
def certify(profile: str = "configs/profiles/<AGENT>.yaml", out: str = "reports/<AGENT>") -> None:
    from probe_engine.config.profile import load_profile, run_config_from_profile
    from probe_engine.corpus.loader import load_corpus
    from probe_engine.mapping.loader import load_crosswalk, load_framework
    from probe_engine.report.builder import build_report
    from probe_engine.report.render_json import render_json
    from probe_engine.report.render_markdown import render_markdown
    from probe_engine.run.executor import run_corpus
    from probe_engine.run.selection import select_probes

    prof = load_profile(profile)
    run_id = uuid.uuid4().hex[:12]
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    rc = run_config_from_profile(prof, run_id, ts)

    probes = load_corpus("corpus/probes")
    fw = load_framework("frameworks/aiuc-1.yaml")
    cw = load_crosswalk("crosswalks/aiuc-1/crosswalk.yaml")
    selected = select_probes(probes, rc)
    print(f"selected {len(selected)}/{len(probes)} probes (industry={rc.industry})")

    evidence = run_corpus(selected, rc, external=external, log_dir=f"logs/{run_id}")

    report = build_report(rc, fw, cw, evidence)
    d = pathlib.Path(out)
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.json").write_text(render_json(report), encoding="utf-8")
    (d / "report.md").write_text(render_markdown(report), encoding="utf-8")
    for e in evidence:
        print(f"  {e.probe_id:36s} {e.scenario:11s} {e.n_success}/{e.n_trials} "
              f"ASR={e.asr:.1%} [{e.status.value}]")
    print(f"report -> {d}")


if __name__ == "__main__":
    certify()
