"""``pap status`` — what has each source been doing lately?

Reads the run ledger and the notification queue. This is the "is it degrading?"
view that complements GlitchTip (which shows crashes) and Healthchecks (which
shows silence): a source that runs successfully but has quietly started finding
zero items is broken in a way neither of the others can see.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..db import Database


def _ago(when: datetime | None) -> str:
    if when is None:
        return "never"
    delta = datetime.now(timezone.utc) - when
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def run_status(db: Database, *, days: int = 7) -> int:
    summaries = db.run_summary(days=days)
    print(f"Runs in the last {days} day(s)")
    if not summaries:
        print("  (no runs recorded)")
    else:
        print(f"  {'source':<14} {'runs':>5} {'fail':>5} {'found':>7} {'new':>6}  "
              f"{'last':<10} status")
        for s in summaries:
            print(f"  {s.source:<14} {s.runs:>5} {s.failures:>5} {s.items_found:>7} "
                  f"{s.items_new:>6}  {_ago(s.last_started_at):<10} {s.last_status or '-'}")

    counts = db.notification_counts()
    print()
    print("Notification queue")
    if not counts:
        print("  (empty)")
    else:
        for state in ("pending", "sent", "failed"):
            if state in counts:
                print(f"  {state:<8} {counts[state]}")

    # A failed notification never retries on its own — it has exhausted its
    # attempts — so it is worth surfacing rather than leaving in the table.
    if counts.get("failed"):
        print()
        print(f"  {counts['failed']} notification(s) gave up after repeated failures. "
              f"Inspect: SELECT dedupe_key, last_error FROM pap.notification WHERE state='failed';")
    return 0
