"""B5: reconcile_channels — cross-check profile-declared delivery channels against
the channels a bridge adapter can actually route poison into (pure, offline)."""

from probe_engine.run.selection import reconcile_channels


def test_routable_none_passthrough():
    # No adapter info: trust the profile, nothing to reconcile.
    result = reconcile_channels({"message", "retrieved_doc", "memory"}, None)
    assert result == {
        "tested": ["memory", "message", "retrieved_doc"],
        "declared_not_routable": [],
        "routable_not_declared": [],
    }


def test_declared_not_routable_detected():
    # Profile declares retrieved_doc but the adapter can't deliver it -> false coverage.
    result = reconcile_channels(
        {"message", "retrieved_doc"}, {"message"}
    )
    assert result["declared_not_routable"] == ["retrieved_doc"]
    assert "retrieved_doc" not in result["tested"]
    assert result["tested"] == ["message"]


def test_routable_not_declared_detected():
    # Adapter could route memory but the profile omits it -> missed coverage. history is a
    # universal and also undeclared here, so it surfaces alongside memory.
    result = reconcile_channels({"message"}, {"message", "memory"})
    assert result["routable_not_declared"] == ["history", "memory"]
    assert result["tested"] == ["message"]
    assert result["declared_not_routable"] == []


def test_message_history_always_routable():
    # message/history are universal: declaring them is never a blind spot even if the
    # adapter's routable set omits them.
    result = reconcile_channels(
        {"message", "history", "tool_result"}, {"tool_result"}
    )
    assert "message" in result["tested"]
    assert "history" in result["tested"]
    assert "tool_result" in result["tested"]
    assert result["declared_not_routable"] == []
    # message/history are not flagged as routable_not_declared either (they're declared here).
    assert result["routable_not_declared"] == []


def test_message_history_universal_show_as_missed_when_undeclared():
    # If the profile omits message/history, they surface as routable_not_declared (the
    # adapter can always route them), never silently dropped.
    result = reconcile_channels({"retrieved_doc"}, {"retrieved_doc"})
    assert set(result["routable_not_declared"]) == {"history", "message"}
    assert result["tested"] == ["retrieved_doc"]


def test_empty_declared_with_routable():
    # No declared channels but adapter routes the universals -> all missed coverage.
    result = reconcile_channels(set(), {"message", "history"})
    assert result["tested"] == []
    assert result["declared_not_routable"] == []
    assert result["routable_not_declared"] == ["history", "message"]


def test_empty_both():
    result = reconcile_channels(set(), None)
    assert result == {
        "tested": [],
        "declared_not_routable": [],
        "routable_not_declared": [],
    }


def test_empty_routable_set_keeps_universals():
    # routable is an empty SET (adapter info present, but no surfaces beyond universals).
    result = reconcile_channels({"message", "memory"}, set())
    assert result["tested"] == ["message"]
    assert result["declared_not_routable"] == ["memory"]
    assert result["routable_not_declared"] == ["history"]


def test_outputs_are_sorted_lists():
    result = reconcile_channels(
        {"tool_result", "message", "file_content"}, {"file_content", "ingested_record"}
    )
    for key in ("tested", "declared_not_routable", "routable_not_declared"):
        assert result[key] == sorted(result[key])
        assert isinstance(result[key], list)
