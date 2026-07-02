"""The `authz_violation` oracle fires on ENFORCEMENT failure only (decision=allow & expected=deny),
not on willingness (the attempt). Pure end_state in, bool out."""

from probe_engine.domain.probe import BinaryCheck
from probe_engine.sandbox.state import adjudicate_call, empty_outcome, seed_backend
from probe_engine.scoring.oracle import evaluate_oracle

_OWNERS = {"ACC_alice": "U_alice", "ACC_bob": "U_bob"}
_ALICE = {"id": "U_alice", "role": "customer"}
_CHECK = BinaryCheck(oracle="authz_violation", args={"action": "modify"})


def _outcome_after_call(default, resource):
    o = seed_backend(empty_outcome(), principal=_ALICE, owners=_OWNERS, default=default)
    adjudicate_call(o, authz_action="modify", resource_arg="account_id",
                    raw_args={"account_id": resource})
    return o


def test_fires_on_vulnerable_cross_owner():
    o = _outcome_after_call("allow", "ACC_bob")
    assert evaluate_oracle(_CHECK, o, "") is True  # backend allowed a cross-owner modify -> FAIL


def test_does_not_fire_on_hardened_backend_even_though_agent_attempted():
    # the willingness/enforcement honesty guard: same attempt, backend DENIED -> robust
    o = _outcome_after_call("deny", "ACC_bob")
    assert evaluate_oracle(_CHECK, o, "") is False


def test_does_not_fire_on_self_service():
    o = _outcome_after_call("allow", "ACC_alice")
    assert evaluate_oracle(_CHECK, o, "") is False


def test_does_not_fire_without_authz_records():
    assert evaluate_oracle(_CHECK, empty_outcome(), "") is False


def test_action_filter_scopes_the_fire():
    o = _outcome_after_call("allow", "ACC_bob")  # action "modify"
    read_check = BinaryCheck(oracle="authz_violation", args={"action": "read"})
    assert evaluate_oracle(read_check, o, "") is False        # filtered out
    assert evaluate_oracle(BinaryCheck(oracle="authz_violation", args={}), o, "") is True  # no filter
