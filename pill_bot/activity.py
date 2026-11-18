"""Privacy-conscious activity recording and usage reports."""

import json
import logging
import re
import time

from .common import ACTIVITY_RETENTION_SECONDS

LOG = logging.getLogger("pill_reminder")


class ActivityMixin:
    def remember_identity(self, profile):
        user_id = profile["id"]
        username = profile.get("username")
        first_name = profile.get("first_name")
        if isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_]{1,32}", username):
            display_name = "@" + username
        elif isinstance(first_name, str) and first_name.strip():
            display_name = first_name.strip()[:64]
        else:
            return
        with self.db:
            self.db.execute(
                """INSERT INTO user_identities VALUES (?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET display_name = excluded.display_name""",
                (user_id, display_name),
            )

    def log_activity(self, user_id, action):
        with self.db:
            self.db.execute(
                "INSERT INTO activity_events (user_id, occurred_at, action) VALUES (?, ?, ?)",
                (user_id, int(time.time()), action),
            )
        row = self.db.execute(
            "SELECT display_name FROM user_identities WHERE user_id = ?", (user_id,)
        ).fetchone()
        identity = json.dumps(row["display_name"], ensure_ascii=True) if row else "unknown"
        LOG.info("User %s (ID %s) %s", identity, user_id, action)

    def prune_activity(self, now):
        with self.db:
            self.db.execute(
                "DELETE FROM activity_events WHERE occurred_at < ?",
                (now - ACTIVITY_RETENTION_SECONDS,),
            )

    def usage_report(self, now):
        rows = self.db.execute(
            """SELECT activity_events.user_id, user_identities.display_name,
                      activity_events.action, COUNT(*) AS count
               FROM activity_events
               LEFT JOIN user_identities ON user_identities.user_id = activity_events.user_id
               WHERE occurred_at >= ?
               GROUP BY activity_events.user_id, activity_events.action
               ORDER BY activity_events.user_id, activity_events.action""",
            (now - ACTIVITY_RETENTION_SECONDS,),
        ).fetchall()
        if not rows:
            return "Usage in the last 90 days: no activity recorded."
        totals = {}
        lines = ["Usage in the last 90 days:"]
        previous = None
        for row in rows:
            if row["user_id"] != previous:
                previous = row["user_id"]
                name = (json.dumps(row["display_name"], ensure_ascii=True)
                        if row["display_name"] else "unknown")
                lines.append(f"\nUser {name} (ID {previous}):")
            lines.append(f"  {row['action']}: {row['count']}")
            totals[row["action"]] = totals.get(row["action"], 0) + row["count"]
        lines.append("\nOverall totals:")
        lines.extend(f"  {action}: {count}" for action, count in sorted(totals.items()))
        return "\n".join(lines)
