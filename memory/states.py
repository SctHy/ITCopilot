"""Memory maintenance stages, independent of the durable fact lifecycle."""
from core.states import Protocol

MAINTENANCE = Protocol("memory_maintenance", "checking", {
    "checking": {"compress": "compressing"},
    "compressing": {"extract": "extracting", "skip": "finished"},
    "extracting": {"commit": "finished", "defer": "deferred"},
})
COMPRESSION = Protocol("compression", "snapshot", {
    "snapshot": {"skip": "skipped", "summarize": "summarizing"},
    "summarizing": {"validate": "validating", "defer": "deferred"},
    "validating": {"conflict": "validating", "commit": "committed", "defer": "deferred"},
    "committed": {"archive": "archived", "defer": "archive_pending"},
})


def profile_phase(count, mark, interval):
    """Compression resets the counter; only a successful extraction advances mark."""
    effective_mark = 0 if count < mark else mark
    return "due" if count - effective_mark >= interval else "waiting"
