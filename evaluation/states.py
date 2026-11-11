"""Evaluation control flow, separate from whether the product passed its checks."""
from core.states import Protocol

CANDIDATE = Protocol("candidate", "proposed", {
    "proposed": {"empty": "no_proposal", "reject": "rejected", "validate": "validated"},
    "validated": {"evaluate": "dev"},
    "dev": {"reject": "rejected", "advance": "holdout_safety"},
    "holdout_safety": {"accept": "accepted", "reject": "rejected"},
})

PROMOTION = Protocol("promotion", "checking", {
    "checking": {"stage": "staged"}, "staged": {"write": "writing"},
    "writing": {"complete": "completed", "fail": "failed"},
})

JUDGE = Protocol("judge", "ready", {
    "ready": {"request": "requesting"},
    "requesting": {"valid": "completed", "retry": "backoff", "fail": "failed", "cancel": "cancelled"},
    "backoff": {"request": "requesting", "cancel": "cancelled"},
})

TRIAL = Protocol("simulated_trial", "dialogue", {
    "dialogue": {"stop": "user_stop", "limit": "max_turns", "fail": "failed"},
    "user_stop": {"approve": "approving", "fail": "failed"},
    "max_turns": {"approve": "approving", "fail": "failed"},
    "approving": {"grade": "grading", "fail": "failed"},
    "grading": {"complete": "completed", "fail": "failed"},
})
