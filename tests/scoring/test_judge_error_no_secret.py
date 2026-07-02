"""B6 follow-up — a judge-resolution failure must NEVER carry a secret to disk.

The B6 fix wired stamp_judge_status into the executor, so meta["judge_error"] (the resolution-error
string) is now PERSISTED to the .eval. If resolve_judge_model embedded the raw exception repr, a
provider client that echoes the api_key in its error would leak it. resolve_judge_model must keep
ONLY the model id (a non-secret slug) + the exception TYPE name (invariant 2). Fully offline.
"""

import pytest

from probe_engine.scoring.batch_judge import (
    JUDGE_STATUS_UNAVAILABLE,
    JUDGE_UNVERIFIED_MARKER,
    BatchJudgeResult,
    stamp_judge_status,
)
from probe_engine.scoring.judge import JudgeUnavailable, resolve_judge_model

_SECRET = "sk-ant-SUPERSECRETKEY-do-not-leak-0xDEADBEEF"


def _resolver_that_leaks(model_id, api_key=None):
    # Mimic a provider client whose exception text embeds the api_key (URLs / auth headers do this).
    raise RuntimeError(f"401 Unauthorized for key={_SECRET} at https://api/v1?key={_SECRET}")


def test_resolution_error_string_has_no_secret():
    with pytest.raises(JudgeUnavailable) as ei:
        resolve_judge_model("anthropic/claude-x", api_key=_SECRET, resolver=_resolver_that_leaks)
    msg = str(ei.value)
    assert _SECRET not in msg                      # the key never appears
    assert "anthropic/claude-x" in msg             # the (non-secret) model id is kept for triage
    assert "RuntimeError" in msg                   # only the exception TYPE name, not its repr


def test_fallback_chain_error_string_has_no_secret():
    with pytest.raises(JudgeUnavailable) as ei:
        resolve_judge_model(
            ["m/primary", "m/fallback"], api_key=_SECRET, resolver=_resolver_that_leaks
        )
    msg = str(ei.value)
    assert _SECRET not in msg
    assert "m/primary" in msg and "m/fallback" in msg


def test_persisted_judge_error_marker_carries_no_secret():
    # The sanitized error flows status -> meta["judge_error"], which the executor persists to disk.
    err = "anthropic/claude-x: RuntimeError"   # what resolve_judge_model now produces (no secret)
    result = BatchJudgeResult(mask=[True], status=JUDGE_STATUS_UNAVAILABLE, error=err)
    metas = [{}]
    stamp_judge_status(metas, result)
    assert metas[0][JUDGE_UNVERIFIED_MARKER] is True
    assert _SECRET not in metas[0].get("judge_error", "")
