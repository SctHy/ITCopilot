"""Unsupported persisted formats and broken evaluation inputs must fail explicitly."""
import json

import pytest

from it_copilot.agent.checkpointing import decode, encode
from it_copilot.agent.types import AgentResponse, Request
from it_copilot.infra.events import StreamConflict, _binding
from tests.support.evaluator import load_intent_cases


@pytest.mark.parametrize("payload", [None, {}, {"cases": []}, {"cases": [{"message": "VPN"}]},
                                     {"cases": [{"message": "", "expected_intent": "vpn_issue"}]}])
def test_invalid_intent_dataset_never_switches_to_a_smaller_suite(tmp_path, payload):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        load_intent_cases(str(path))


def test_missing_and_malformed_intent_dataset_fail(tmp_path):
    path = tmp_path / "cases.json"
    with pytest.raises(FileNotFoundError):
        load_intent_cases(str(path))
    path.write_text("{broken")
    with pytest.raises(ValueError):
        load_intent_cases(str(path))


def test_current_checkpoint_restores_output_state_and_requires_it():
    original = Request("VPN", conversation_state={"stage": "after"})
    saved = encode(AgentResponse("done", True), original)
    resumed = Request("VPN", conversation_state={"stage": "before"})
    assert decode(saved, resumed).content == "done"
    assert resumed.conversation_state == {"stage": "after"}
    del saved["conversation_state"]
    with pytest.raises(KeyError, match="conversation_state"):
        decode(saved, resumed)


@pytest.mark.parametrize("raw", ["owner-only", '"owner-only"', '{"owner":"alice"}'])
def test_event_binding_requires_current_owner_and_fingerprint_envelope(raw):
    with pytest.raises(StreamConflict):
        _binding(raw)
    assert _binding('{"owner":"alice","fingerprint":"input-hash"}') == {
        "owner": "alice", "fingerprint": "input-hash"}
