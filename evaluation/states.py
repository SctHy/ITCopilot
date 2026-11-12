"""Evaluation control flow, separate from whether the product passed its checks."""
from core.states import Protocol

TRIAL = Protocol("simulated_trial", "dialogue", {
    "dialogue": {"stop": "user_stop", "limit": "max_turns", "fail": "failed"},
    "user_stop": {"approve": "approving", "fail": "failed"},
    "max_turns": {"approve": "approving", "fail": "failed"},
    "approving": {"grade": "grading", "fail": "failed"},
    "grading": {"complete": "completed", "fail": "failed"},
})
