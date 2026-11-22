"""Reminder persistence and scheduled dose delivery."""

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .common import TIMEZONE_PROMPT, buttons, next_occurrence
from .telegram import TelegramError

LOG = logging.getLogger("pill_reminder")


class ReminderMixin:
    def bind_database(self):
        with self.db:
            bot_id = self.token.split(":", 1)[0]
            existing = self.db.execute(
                "SELECT value FROM settings WHERE key = 'bot_id'"
            ).fetchone()
            if existing and existing["value"] != bot_id:
                raise ValueError("Database belongs to a different bot_id; use another PILL_DB.")
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('bot_id', ?)", (bot_id,))
            migrated = self.db.execute(
                "SELECT value FROM settings WHERE key = 'user_timezones_migrated'"
            ).fetchone()
            if not migrated:
                previous = self.db.execute(
                    "SELECT value FROM settings WHERE key = 'timezone'"
                ).fetchone()
                has_reminders = self.db.execute("SELECT 1 FROM reminders LIMIT 1").fetchone()
                if has_reminders and not previous:
                    raise ValueError("Cannot migrate reminders: database has no previous time zone.")
                if previous:
                    try:
                        ZoneInfo(previous["value"])
                    except (ValueError, ZoneInfoNotFoundError):
                        raise ValueError("Cannot migrate reminders: invalid previous time zone.") from None
                    self.db.execute(
                        """INSERT OR IGNORE INTO users (user_id, timezone)
                           SELECT DISTINCT user_id, ? FROM reminders""", (previous["value"],)
                    )
                self.db.execute("INSERT INTO settings VALUES ('user_timezones_migrated', '1')")
        self.privacy.initialize(self.db)

    def user_zone(self, user_id):
        row = self.db.execute("SELECT timezone FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not row or not row["timezone"]:
            raise ValueError(TIMEZONE_PROMPT)
        return ZoneInfo(row["timezone"])

    def set_timezone(self, user_id, name, now):
        try:
            zone = ZoneInfo(name)
        except (ValueError, ZoneInfoNotFoundError, IsADirectoryError):
            self.prompt_timezone(user_id)
            raise ValueError("Unknown time zone. " + TIMEZONE_PROMPT) from None
        existing = self.db.execute("SELECT timezone FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if existing and existing["timezone"] and existing["timezone"] != zone.key:
            self.create_due(now)
        with self.db:
            self.db.execute(
                """INSERT INTO users (user_id, timezone, awaiting_timezone) VALUES (?, ?, 0)
                   ON CONFLICT(user_id) DO UPDATE SET timezone = excluded.timezone,
                   awaiting_timezone = 0""", (user_id, zone.key)
            )
            if existing and existing["timezone"] and existing["timezone"] != zone.key:
                slots = self.db.execute(
                    """SELECT schedules.id, schedules.clock FROM schedules JOIN reminders
                       ON reminders.id = schedules.reminder_id WHERE reminders.user_id = ?""",
                    (user_id,)
                ).fetchall()
                for slot in slots:
                    self.db.execute("UPDATE schedules SET next_due = ? WHERE id = ?",
                                    (next_occurrence(slot["clock"], now, zone), slot["id"]))
        self.log_activity(user_id, "set their time zone")
        return f"Time zone set to {zone.key}. Daily reminder times stay the same local clock times."

    def prompt_timezone(self, user_id):
        with self.db:
            self.db.execute(
                """INSERT INTO users (user_id, awaiting_timezone) VALUES (?, 1)
                   ON CONFLICT(user_id) DO UPDATE SET awaiting_timezone = 1""", (user_id,)
            )
        return TIMEZONE_PROMPT

    def add(self, user_id, name, clocks, now, *, finish_flow=False):
        zone = self.user_zone(user_id)
        with self.db:
            self.check_duplicate(user_id, name)
            result = self.db.execute("INSERT INTO reminders(user_id, name) VALUES (?, ?)",
                                     (user_id, self.privacy.encrypt(name)))
            reminder_id = result.lastrowid
            self.db.executemany(
                "INSERT INTO schedules(reminder_id, clock, next_due) VALUES (?, ?, ?)",
                [(reminder_id, clock, next_occurrence(clock, now, zone))
                 for clock in clocks],
            )
            if finish_flow:
                self.db.execute("DELETE FROM conversations WHERE user_id = ?", (user_id,))
        self.log_activity(user_id, "added a medication")
        return reminder_id

    def edit(self, user_id, reminder_id, name, clocks, now, *, finish_flow=False):
        zone = self.user_zone(user_id)
        with self.db:
            row = self.db.execute(
                "SELECT id, name FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, user_id)
            ).fetchone()
            if not row:
                return False
            if name != self.privacy.decrypt(row["name"]):
                self.check_duplicate(user_id, name, reminder_id)
            self.db.execute("UPDATE reminders SET name = ? WHERE id = ?",
                            (self.privacy.encrypt(name), reminder_id))
            existing = {slot["clock"]: slot["id"] for slot in self.db.execute(
                "SELECT id, clock FROM schedules WHERE reminder_id = ?", (reminder_id,)
            )}
            desired = set(clocks)
            self.db.executemany("DELETE FROM schedules WHERE id = ?",
                                [(slot_id,) for clock, slot_id in existing.items()
                                 if clock not in desired])
            self.db.executemany(
                "INSERT INTO schedules(reminder_id, clock, next_due) VALUES (?, ?, ?)",
                [(reminder_id, clock, next_occurrence(clock, now, zone))
                 for clock in clocks if clock not in existing],
            )
            if finish_flow:
                self.db.execute("DELETE FROM conversations WHERE user_id = ?", (user_id,))
        self.log_activity(user_id, "edited a medication")
        return True

    def check_duplicate(self, user_id, name, exclude_id=None):
        rows = self.db.execute("SELECT id, name FROM reminders WHERE user_id = ?", (user_id,))
        if any(row["id"] != exclude_id and self.privacy.decrypt(row["name"]).strip().casefold() == name.strip().casefold()
               for row in rows):
            raise ValueError("You already have a medication with that name. Enter a different name.")

    def set_enabled(self, user_id, reminder_id, enabled, now):
        zone = self.user_zone(user_id)
        with self.db:
            row = self.db.execute(
                "SELECT enabled FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, user_id)
            ).fetchone()
            if not row:
                return False
            if row["enabled"] != int(enabled):
                self.db.execute(
                    "UPDATE reminders SET enabled = ? WHERE id = ?", (int(enabled), reminder_id)
                )
                if enabled:
                    for slot in self.db.execute(
                        "SELECT id, clock FROM schedules WHERE reminder_id = ?", (reminder_id,)
                    ).fetchall():
                        self.db.execute(
                            "UPDATE schedules SET next_due = ? WHERE id = ?",
                            (next_occurrence(slot["clock"], now, zone), slot["id"]),
                        )
        if row["enabled"] != int(enabled):
            self.log_activity(user_id, "enabled a medication" if enabled else "paused a medication")
        return True

    def remove(self, user_id, reminder_id):
        with self.db:
            removed = self.db.execute(
                "DELETE FROM reminders WHERE id = ? AND user_id = ?",
                (reminder_id, user_id),
            ).rowcount
        if removed:
            self.log_activity(user_id, "removed a medication")
        return bool(removed)

    def forget(self, user_id):
        with self.db:
            owned = "reminder_id IN (SELECT id FROM reminders WHERE user_id = ?)"
            self.db.execute("DELETE FROM doses WHERE " + owned, (user_id,))
            self.db.execute("DELETE FROM schedules WHERE " + owned, (user_id,))
            for table in ("reminders", "conversations", "reply_prompts", "users",
                          "user_identities", "activity_events"):
                self.db.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
            # Left over from migrating a single-owner database.
            self.db.execute(
                """DELETE FROM settings WHERE key IN ('owner', 'timezone') AND EXISTS
                   (SELECT 1 FROM settings WHERE key = 'owner' AND value = ?)""",
                (str(user_id),),
            )
        LOG.info("User ID %s deleted all their data", user_id)

    def create_due(self, now):
        with self.db:
            slots = self.db.execute(
                """SELECT schedules.*, users.timezone FROM schedules JOIN reminders
                   ON reminders.id = schedules.reminder_id
                   JOIN users ON users.user_id = reminders.user_id
                   WHERE reminders.enabled = 1 AND next_due <= ?""", (now,)
            ).fetchall()
            for slot in slots:
                due = slot["next_due"]
                zone = ZoneInfo(slot["timezone"])
                while due <= now:
                    self.db.execute(
                        "INSERT INTO doses(reminder_id, due, next_alert) VALUES (?, ?, ?)",
                        (slot["reminder_id"], due, now),
                    )
                    due = next_occurrence(slot["clock"], due, zone)
                self.db.execute("UPDATE schedules SET next_due = ? WHERE id = ?", (due, slot["id"]))

    def dose_message(self, dose):
        scheduled = datetime.fromtimestamp(dose["due"], ZoneInfo(dose["timezone"]))
        text = f"Medication reminder: {self.privacy.decrypt(dose['name'])}\nScheduled: {scheduled:%Y-%m-%d %H:%M %Z}"
        markup = buttons([("✅ Taken", f"taken:{dose['id']}")])
        return text, markup

    def tick(self, now):
        self.create_due(now)
        doses = self.db.execute(
            """SELECT doses.id, doses.due, reminders.name, reminders.user_id, users.timezone
               FROM doses JOIN reminders
               ON reminders.id = doses.reminder_id
               JOIN users ON users.user_id = reminders.user_id
               WHERE reminders.enabled = 1 AND doses.next_alert <= ?
               ORDER BY doses.next_alert, doses.id LIMIT 20""", (now,)
        ).fetchall()
        for dose in doses:
            try:
                self.send(*self.dose_message(dose), chat_id=dose["user_id"])
            except TelegramError as exc:
                LOG.warning("Could not send dose %s: %s; retrying in one minute", dose["id"], exc)
                next_alert = now + 60
            else:
                next_alert = int(time.time()) + 3600
                self.log_activity(dose["user_id"], "was sent a medication reminder")
            with self.db:
                self.db.execute("UPDATE doses SET next_alert = ? WHERE id = ?",
                                (next_alert, dose["id"]))
