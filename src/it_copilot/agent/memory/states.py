"""Memory maintenance stages, independent of the durable fact lifecycle."""
from it_copilot.infra.states import Protocol

COMPRESSION = Protocol("compression", "snapshot", {
    "snapshot": {"skip": "skipped", "summarize": "summarizing"},
    "summarizing": {"validate": "validating", "defer": "deferred"},
    "validating": {"conflict": "validating", "commit": "committed", "defer": "deferred"},
    "committed": {"archive": "archived", "defer": "archive_pending"},
})
