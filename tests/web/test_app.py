import time
from pathlib import Path

from fastapi.testclient import TestClient

from probe_engine.web.app import RunRequest, _resolve_key, create_app, run_scan

ROOT = Path(__file__).parents[2]
CORPUS = str(ROOT / "corpus" / "probes")
FW = str(ROOT / "frameworks" / "aiuc-1.yaml")
CW = str(ROOT / "crosswalks" / "aiuc-1" / "crosswalk.yaml")
SECRET = "sk-SECRET-DO-NOT-LEAK-123"


def _req(**over):
    base = dict(tier="mock", mock_rule="always", n_variants=2, epochs=1,
                corpus=CORPUS, framework=FW, crosswalk=CW)
    base.update(over)
    return RunRequest(**base)


def test_run_scan_emits_progress_and_builds_report(tmp_path):
    events = []
    report = run_scan(_req(api_key=SECRET), lambda k, d: events.append((k, d)),
                      log_dir=str(tmp_path / "logs"))
    kinds = [k for k, _ in events]
    assert kinds[0] == "started"
    assert "probe_start" in kinds and "probe_done" in kinds
    assert kinds[-1] == "done"
    started = next(d for k, d in events if k == "started")
    assert started["selected"] == 96
    assert sum(1 for k, _ in events if k == "probe_done") == 96
    assert report["aggregates"]["n_probes"] == 96
    assert report["aggregates"]["overall_asr"] == 1.0  # mock rule "always"


def test_api_key_never_leaks(tmp_path):
    events = []
    report = run_scan(_req(api_key=SECRET), lambda k, d: events.append((k, d)),
                      log_dir=str(tmp_path / "logs"))
    blob = repr(events) + repr(report)
    assert SECRET not in blob


def test_endpoints_smoke():
    client = TestClient(create_app())
    assert client.get("/").status_code == 200
    assert "Probe Engine" in client.get("/").text
    meta = client.get("/meta", params={"corpus": CORPUS}).json()
    assert meta["probe_count"] == 118


def test_run_endpoint_to_report():
    client = TestClient(create_app())
    r = client.post("/run", json=_req(api_key=SECRET).model_dump())
    run_id = r.json()["run_id"]
    for _ in range(160):
        if client.get(f"/status/{run_id}").json()["done"]:
            break
        time.sleep(0.25)
    rep = client.get(f"/report/{run_id}")
    assert rep.status_code == 200
    assert rep.json()["aggregates"]["n_probes"] == 96
    assert SECRET not in rep.text


def test_resolve_key_prefers_request_then_env(monkeypatch):
    # explicit api_key wins
    assert _resolve_key(_req(api_key=SECRET)) == SECRET
    # keys-in-env path: no secret in the body, server reads the named env var
    monkeypatch.setenv("MY_TARGET_KEY", SECRET)
    assert _resolve_key(_req(api_key_env="MY_TARGET_KEY")) == SECRET
    # fallback to OPENROUTER_API_KEY
    monkeypatch.delenv("MY_TARGET_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fallback")
    assert _resolve_key(_req()) == "sk-or-fallback"
    # nothing set -> None (offline mock run needs no key)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert _resolve_key(_req()) is None


def test_run_via_env_key_never_puts_secret_in_body(monkeypatch, tmp_path):
    # The request body carries only the env-var NAME; the server resolves the value in-process.
    monkeypatch.setenv("MY_TARGET_KEY", SECRET)
    req = _req(api_key_env="MY_TARGET_KEY")
    assert SECRET not in req.model_dump_json()
    events = []
    report = run_scan(req, lambda k, d: events.append((k, d)), log_dir=str(tmp_path / "logs"))
    assert SECRET not in (repr(events) + repr(report))


def test_fail_fast_and_judge_thread_into_run_config(monkeypatch, tmp_path):
    """The integrated launch must honor the intake's early-exit + judge choices: web RunRequest
    fields reach the RunConfig run_probe sees."""
    import probe_engine.web.app as appmod
    real = appmod.run_probe
    seen = {}

    def spy(probe, run_config, **kw):
        seen["fail_fast"] = run_config.fail_fast
        seen["judge"] = run_config.target.judge_model
        return real(probe, run_config, **kw)

    monkeypatch.setattr(appmod, "run_probe", spy)
    run_scan(_req(api_key=SECRET, fail_fast=True, judge_model="openrouter/x-judge"),
             lambda k, d: None, log_dir=str(tmp_path / "logs"))
    assert seen["fail_fast"] is True
    assert seen["judge"] == "openrouter/x-judge"


def test_watch_page():
    client = TestClient(create_app())
    assert client.get("/watch/nonexistent").status_code == 404
    run_id = client.post("/run", json=_req(api_key=SECRET).model_dump()).json()["run_id"]
    w = client.get(f"/watch/{run_id}")
    assert w.status_code == 200
    assert run_id in w.text and "PharosOne" in w.text
    assert 'EventSource("/events/"+rid)' in w.text  # attaches to the live SSE stream
