"""Channel routing in the bridge: a poison turn on an ingestion channel (`ingested_record`) is
handed to the agent as `request['injection'] = {channel, payload}` — NOT inlined into the message
text — so the adapter can route it into the agent's real surface (card field). A default
(`message`) poison turn keeps riding inside the message text. Offline: a spy `external`."""

from probe_engine.corpus.loader import load_corpus
from probe_engine.domain.run import RunConfig, TargetConfig, Thresholds, ToolSpec
from probe_engine.run.executor import run_probe

CORPUS = "corpus/probes"
_STATUS = ToolSpec(name="set_status", capabilities=["status_change"], dangerous=True)


def _probe(pid: str):
    return next(p for p in load_corpus(CORPUS) if p.id == pid)


def _rc(channels):
    return RunConfig(
        target=TargetConfig(tier="bridge", channels=channels),
        tool_inventory=[_STATUS],
        n_variants=1,
        epochs=1,
        thresholds=Thresholds(asr_pass=0.05, target_asr=0.05),
        run_id="r",
        timestamp="2026-06-23T00:00:00Z",
    )


def _spy():
    seen: list[dict] = []

    async def ext(request):
        seen.append(request)
        return {"choices": [{"message": {"role": "assistant", "content": "I can't act on that."}}]}

    return ext, seen


def test_ingested_record_poison_is_routed_to_injection_not_the_message():
    ext, seen = _spy()
    run_probe(_probe("indirect-status-via-record"),
              _rc(["message", "history", "ingested_record"]), external=ext)
    assert seen, "external was never called"
    injected = [r for r in seen if "injection" in r]
    assert injected, "no request carried an injection directive"
    inj = injected[0]["injection"]
    assert inj["channel"] == "ingested_record"
    assert "PHAROS-CANARY-REC-7F3X" in inj["payload"]
    # the poison must NOT be inlined into the conversation text on the injection channel
    blob = "".join(m.get("content", "") for r in injected for m in r["messages"])
    assert "PHAROS-CANARY-REC-7F3X" not in blob


def test_message_channel_poison_stays_inline():
    ext, seen = _spy()
    # generic-status-manipulation delivers on the default message channel
    run_probe(_probe("generic-status-manipulation"), _rc(["message", "history"]), external=ext)
    assert seen
    assert not any("injection" in r for r in seen), "message-channel poison must not use injection"
    blob = "".join(m.get("content", "") for r in seen for m in r["messages"])
    assert "PHAROS-CANARY-STATUS-9K2D" in blob  # inlined as [external content]
