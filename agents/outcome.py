"""The shared definition used by online records, evaluations and release checks."""
from collections.abc import Mapping


def turn_outcome(record: Mapping) -> str:
    signals = record.get("signals", ())
    code = record.get("error_code")
    if "claim_noted" in signals or code == "false_claim_noted":
        return "false_claim_noted"
    if "round_limit" in signals or code == "round_limit":
        return "round_limit"
    if "model_unavailable" in signals or code == "model_unavailable":
        return "model_unavailable"
    status = record["status"]
    return status if status in {"success", "partial"} else "failed"
