import unittest
import sqlite3
import json
import logging
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zoneinfo import ZoneInfo

import requests
from cryptography.fernet import Fernet

from pill_reminder import (ACTIVITY_RETENTION_SECONDS, ReminderBot, TelegramError,
                           TelegramResponseError, connect, occurrence, parse_clock, parse_times)
from pill_bot.privacy import EncryptionError


ZONE = ZoneInfo("Europe/London")


def stamp(year, month, day, hour, minute=0):
    return int(datetime(year, month, day, hour, minute, tzinfo=ZONE).timestamp())


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.key = Fernet.generate_key().decode("ascii")
        self.key_patch = patch.dict(os.environ, {"PILL_ENCRYPTION_KEY": self.key})
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)
        self.db = connect(":memory:")
        self.bot = ReminderBot("123:token", self.db)
        self.bot.bind_database()
        self.bot.set_timezone(42, "Europe/London", 0)
        self.bot.set_timezone(99, "Europe/London", 0)
        self.sent = []
        def send(*args, **kwargs):
            self.sent.append((args, kwargs))
            return {"message_id": len(self.sent)}
        self.bot.send = send

    def tearDown(self):
        self.bot.http.close()
        self.db.close()

    def doses(self):
        return self.db.execute("SELECT * FROM doses ORDER BY id").fetchall()

    def test_encryption_migrates_existing_database_and_conversation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "reminders.sqlite3"
            db = connect(str(path))
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                db.execute("INSERT INTO settings VALUES ('user_timezones_migrated', '1')")
                db.execute("INSERT INTO users (user_id, timezone) VALUES (42, 'Europe/London')")
                db.execute("INSERT INTO reminders (user_id, name) VALUES (42, 'PrivateMigrationMedicine')")
                db.execute("INSERT INTO schedules (reminder_id, clock, next_due) VALUES (1, '08:00', ?)",
                           (stamp(2026, 1, 1, 8),))
                db.execute("INSERT INTO doses (reminder_id, due, next_alert) VALUES (1, ?, ?)",
                           (stamp(2026, 1, 1, 8), stamp(2026, 1, 1, 8)))
                db.execute("INSERT INTO conversations VALUES (42, 'time_input', ?)",
                           (json.dumps({"name": "PrivateFlowMedicine", "times": [], "mode": "add"}),))
            bot = ReminderBot("123:token", db)
            bot.bind_database()
            self.assertIn("PrivateMigrationMedicine", bot.command(42, "/list", 0))
            self.assertEqual(bot.flow(42)[1]["name"], "PrivateFlowMedicine")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM doses").fetchone()[0], 1)
            for file in Path(directory).iterdir():
                if file.is_file():
                    content = file.read_bytes()
                    self.assertNotIn(b"PrivateMigrationMedicine", content, file.name)
                    self.assertNotIn(b"PrivateFlowMedicine", content, file.name)
            bot.http.close()
            db.close()
            db = connect(str(path))
            restarted = ReminderBot("123:token", db)
            restarted.bind_database()
            self.assertIn("PrivateMigrationMedicine", restarted.command(42, "/list", 0))
            self.assertEqual(restarted.flow(42)[1]["name"], "PrivateFlowMedicine")
            restarted.http.close()
            db.close()

    def test_encryption_new_data_wrong_key_and_corruption(self):
        self.bot.add(42, "PrivateNewMedicine", ["08:00"], stamp(2026, 1, 1, 7))
        self.bot.set_flow(42, "time_input",
                          {"name": "PrivateDraftMedicine", "times": [], "mode": "add"})
        self.assertNotIn("PrivateNewMedicine", self.db.execute(
            "SELECT name FROM reminders").fetchone()[0])
        self.assertNotIn("PrivateDraftMedicine", self.db.execute(
            "SELECT data FROM conversations").fetchone()[0])
        with patch.dict(os.environ, {"PILL_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii")}):
            wrong = ReminderBot("123:token", self.db)
            try:
                with self.assertRaises(EncryptionError):
                    wrong.bind_database()
            finally:
                wrong.http.close()
        self.assertIn("PrivateNewMedicine", self.bot.command(42, "/list", 0))
        with self.db:
            self.db.execute("UPDATE reminders SET name = 'corrupt'")
        with self.assertRaises(EncryptionError):
            self.bot.command(42, "/list", 0)
        with patch.dict(os.environ, {"PILL_ENCRYPTION_KEY": ""}):
            with self.assertRaises(EncryptionError):
                ReminderBot("123:token", self.db)

    def test_encryption_interrupted_cleanup_resumes_on_restart(self):
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "reminders.sqlite3")
            db = connect(path)
            db.execute("INSERT INTO settings VALUES ('user_timezones_migrated', '1')")
            db.execute("INSERT INTO reminders (user_id, name) VALUES (42, 'OldPrivateMedicine')")
            db.commit()
            bot = ReminderBot("123:token", db)
            with patch.object(bot.privacy, "_checkpoint", side_effect=EncryptionError("busy")):
                with self.assertRaises(EncryptionError):
                    bot.bind_database()
            self.assertIsNotNone(db.execute(
                "SELECT 1 FROM settings WHERE key = 'encryption_cleanup_pending'").fetchone())
            bot.bind_database()
            self.assertIsNone(db.execute(
                "SELECT 1 FROM settings WHERE key = 'encryption_cleanup_pending'").fetchone())
            self.assertNotIn(b"OldPrivateMedicine", Path(path).read_bytes())
            self.assertEqual(bot.privacy.decrypt(
                db.execute("SELECT name FROM reminders").fetchone()[0]), "OldPrivateMedicine")
            bot.http.close()
            db.close()

    def test_symlinked_entry_point_loads_adjacent_package(self):
        source = Path(__file__).resolve().with_name("pill_reminder.py")
        with TemporaryDirectory() as directory:
            link = Path(directory) / "pill_reminder.py"
            try:
                link.symlink_to(source)
            except OSError as exc:
                self.skipTest(f"Creating a symlink is unavailable: {exc}")
            env = os.environ.copy()
            env.pop("BOT_TOKEN", None)
            env.pop("PYTHONPATH", None)
            result = subprocess.run(
                [sys.executable, str(link)], cwd=directory, env=env,
                capture_output=True, text=True, timeout=15,
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Set BOT_TOKEN to your Telegram bot token.", result.stderr)

    def test_times_and_ids_validation(self):
        self.assertEqual(parse_times("20:00, 08:30"), ["08:30", "20:00"])
        for value in ("", "24:00", "8:00", "08:00,08:00", "09:60"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_times(value)
        for value in ("0", "9999999999999999999999", "not-an-id"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.bot.command(42, "/remove " + value, 0)

    def test_offline_catchup_and_idempotence(self):
        self.bot.add(42, "Pill", ["08:00", "20:00"], stamp(2026, 1, 1, 7))
        now = stamp(2026, 1, 3, 9)
        self.bot.create_due(now)
        self.bot.create_due(now)
        self.assertEqual([d["due"] for d in self.doses()], [
            stamp(2026, 1, 1, 8), stamp(2026, 1, 2, 8), stamp(2026, 1, 3, 8),
            stamp(2026, 1, 1, 20), stamp(2026, 1, 2, 20),
        ])

    def test_hourly_repeat_taken_and_independent_doses(self):
        self.bot.add(42, "Pill", ["08:00", "09:00"], stamp(2026, 1, 1, 7))
        at_eight = stamp(2026, 1, 1, 8)
        with patch("pill_reminder.time.time", return_value=at_eight):
            self.bot.tick(at_eight)
        self.assertEqual(len(self.sent), 1)
        self.bot.tick(at_eight + 3599)
        self.assertEqual(len(self.sent), 1)
        with patch("pill_reminder.time.time", return_value=at_eight + 3600):
            self.bot.tick(at_eight + 3600)
        self.assertEqual(len(self.sent), 3)
        dose_id = self.doses()[0]["id"]
        calls = []
        self.bot.api = lambda method, **kw: calls.append((method, kw))
        self.bot.process_update({"callback_query": {
            "id": "click", "from": {"id": 42}, "data": f"taken:{dose_id}",
            "message": {"message_id": 17, "text": "Medication reminder: Pill\nScheduled: 2026-01-01 08:00 GMT",
                        "chat": {"id": 42, "type": "private"}},
        }}, at_eight + 3601)
        self.assertEqual(calls[-1][1]["text"], "Marked taken.")
        self.assertEqual(calls[0], ("editMessageText", {
            "chat_id": 42, "message_id": 17,
            "text": "Medication reminder: Pill\nScheduled: 2026-01-01 08:00 GMT\nTaken: 2026-01-01 09:00:01 GMT",
            "reply_markup": {"inline_keyboard": []},
        }))
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(len(self.doses()), 1)
        with patch("pill_reminder.time.time", return_value=at_eight + 7200):
            self.bot.tick(at_eight + 7200)
        self.assertEqual(len(self.sent), 4)

    def test_pause_resume_without_disabled_backfill(self):
        reminder = self.bot.add(42, "Pill", ["08:00"], stamp(2026, 1, 1, 7))
        self.bot.create_due(stamp(2026, 1, 1, 8))
        self.bot.set_enabled(42, reminder, False, stamp(2026, 1, 1, 9))
        self.bot.tick(stamp(2026, 1, 3, 10))
        self.assertEqual(len(self.doses()), 1)
        self.assertEqual(len(self.sent), 0)
        self.bot.set_enabled(42, reminder, True, stamp(2026, 1, 3, 10))
        with patch("pill_reminder.time.time", return_value=stamp(2026, 1, 3, 10)):
            self.bot.tick(stamp(2026, 1, 3, 10))
        self.assertEqual(len(self.sent), 1)
        self.bot.create_due(stamp(2026, 1, 4, 8))
        self.assertEqual(len(self.doses()), 2)

    def test_edit_preserves_pending_remove_deletes(self):
        reminder = self.bot.add(42, "Pill", ["08:00"], stamp(2026, 1, 1, 7))
        self.bot.create_due(stamp(2026, 1, 1, 8))
        self.assertTrue(self.bot.edit(42, reminder, "New name", ["10:00"], stamp(2026, 1, 1, 9)))
        self.bot.create_due(stamp(2026, 1, 1, 10))
        self.assertEqual(len(self.doses()), 2)
        self.bot.command(42, f"/remove {reminder}", stamp(2026, 1, 1, 10))
        self.assertEqual(len(self.doses()), 0)

    def test_failed_delivery_retries(self):
        self.bot.add(42, "Pill", ["08:00"], stamp(2026, 1, 1, 7))
        now = stamp(2026, 1, 1, 8)
        def fail(*args, **kwargs):
            raise TelegramError("unavailable")
        self.bot.send = fail
        with self.assertLogs("pill_reminder", level="WARNING"):
            self.bot.tick(now)
        self.assertEqual(self.doses()[0]["next_alert"], now + 60)

    def test_unauthorized_callback_does_not_remove_dose(self):
        self.bot.add(42, "Pill", ["08:00"], stamp(2026, 1, 1, 7))
        self.bot.create_due(stamp(2026, 1, 1, 8))
        dose_id = self.doses()[0]["id"]
        calls = []
        self.bot.api = lambda method, **kw: calls.append((method, kw))
        self.bot.process_update({"callback_query": {
            "id": "click", "from": {"id": 99}, "data": f"taken:{dose_id}",
            "message": {"chat": {"id": 99, "type": "private"}},
        }}, stamp(2026, 1, 1, 8))
        self.assertEqual(len(self.doses()), 1)
        self.assertEqual(calls[-1][1]["text"], "Already taken or removed.")
        self.assertEqual(self.sent, [])

    def test_taken_button_repeated_and_edit_failure(self):
        self.bot.add(42, "Pill", ["08:00"], stamp(2026, 1, 1, 7))
        now = stamp(2026, 1, 1, 8)
        self.bot.create_due(now)
        dose_id = self.doses()[0]["id"]
        calls = []
        def api(method, **kw):
            calls.append((method, kw))
            if method == "editMessageText":
                raise TelegramError("unavailable")
        self.bot.api = api
        update = {"callback_query": {
            "id": "click", "from": {"id": 42}, "data": f"taken:{dose_id}",
            "message": {"message_id": 17, "chat": {"id": 42, "type": "private"}},
        }}
        with self.assertRaises(TelegramError):
            self.bot.process_update(update, now)
        self.assertEqual(len(self.doses()), 0)
        self.assertEqual(calls[0][1]["text"],
                         "Medication reminder: Pill\nScheduled: 2026-01-01 08:00 GMT\n"
                         "Taken: 2026-01-01 08:00:00 GMT")
        self.assertEqual(calls[-1][1]["text"], "Marked taken.")
        self.bot.process_update(update, now)
        self.assertEqual(calls[-1][1]["text"], "Already taken or removed.")
        self.assertEqual(self.sent, [])

    def test_taken_preserves_original_paused_message_and_uses_user_timezone(self):
        self.bot.set_timezone(42, "America/New_York", 0)
        now = stamp(2026, 1, 1, 13)
        reminder = self.bot.add(42, "Pill", ["08:00"], now - 3600)
        self.bot.create_due(now)
        self.bot.set_enabled(42, reminder, False, now)
        self.bot.show_pending(42)
        original = self.sent[-1][0][0]
        self.assertIn("\nPaused", original)
        dose_id = self.doses()[0]["id"]
        calls = []
        self.bot.api = lambda method, **kw: calls.append((method, kw))
        self.bot.process_update({"callback_query": {
            "id": "click", "from": {"id": 42}, "data": f"taken:{dose_id}",
            "message": {"message_id": 19, "text": original,
                        "chat": {"id": 42, "type": "private"}},
        }}, now + 600)
        self.assertEqual(calls[0][1]["text"], original + "\nTaken: 2026-01-01 08:10:00 EST")
        self.assertEqual(calls[0][1]["reply_markup"], {"inline_keyboard": []})
        self.assertEqual(len(self.sent), 1)

    def test_london_daylight_saving(self):
        spring = occurrence(datetime(2026, 3, 29).date(), "01:30", ZONE)
        autumn = occurrence(datetime(2026, 10, 25).date(), "01:30", ZONE)
        self.assertEqual(datetime.fromtimestamp(spring, ZONE).strftime("%H:%M %Z"), "02:00 BST")
        self.assertEqual(datetime.fromtimestamp(autumn, ZONE).strftime("%H:%M %Z"), "01:30 BST")

    def test_database_identity(self):
        with self.assertRaises(ValueError):
            ReminderBot("456:token", self.db).bind_database()

    def test_restart_recovers_missed_and_preserves_pending(self):
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "reminders.sqlite3")
            db = connect(path)
            bot = ReminderBot("123:token", db)
            bot.bind_database()
            bot.set_timezone(42, "Europe/London", 0)
            bot.add(42, "Pill", ["08:00"], stamp(2026, 1, 1, 7))
            bot.create_due(stamp(2026, 1, 1, 8))
            db.close()

            db = connect(path)
            try:
                bot = ReminderBot("123:token", db)
                bot.bind_database()
                bot.create_due(stamp(2026, 1, 3, 9))
                rows = db.execute("SELECT due FROM doses ORDER BY due").fetchall()
                self.assertEqual([row["due"] for row in rows], [
                    stamp(2026, 1, 1, 8), stamp(2026, 1, 2, 8), stamp(2026, 1, 3, 8),
                ])
            finally:
                db.close()

    def test_private_users_and_id(self):
        self.bot.process_update({"message": {
            "chat": {"id": 99, "type": "private"}, "from": {"id": 99},
            "text": "/add Other | 08:00",
        }}, stamp(2026, 1, 1, 7))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0], 1)
        self.bot.process_update({"message": {
            "chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": "/id",
        }}, stamp(2026, 1, 1, 7))
        self.assertEqual(self.sent[-1][1]["chat_id"], 42)
        self.assertIn("42", self.sent[-1][0][0])

    def test_tenant_isolation_and_delivery_routing(self):
        first = self.bot.add(42, "First", ["08:00"], stamp(2026, 1, 1, 7))
        second = self.bot.add(99, "Second", ["08:00"], stamp(2026, 1, 1, 7))
        self.assertIn("First", self.bot.command(42, "/list", 0))
        self.assertNotIn("Second", self.bot.command(42, "/list", 0))
        self.assertEqual(self.bot.command(99, f"/disable {first}", 0), "Reminder not found.")
        self.assertEqual(self.bot.command(99, f"/edit {first} | Changed | 09:00", 0),
                         "Reminder not found.")
        self.assertEqual(self.bot.command(99, f"/remove {first}", 0), "Reminder not found.")
        now = stamp(2026, 1, 1, 8)
        with patch("pill_reminder.time.time", return_value=now):
            self.bot.tick(now)
        self.assertEqual({entry[1]["chat_id"] for entry in self.sent}, {42, 99})
        self.sent.clear()
        self.bot.show_pending(42)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("First", self.sent[0][0][0])
        self.assertEqual(self.sent[0][1]["chat_id"], 42)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0], 2)
        self.assertNotEqual(first, second)

    def test_migrates_existing_single_owner_database(self):
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "old.sqlite3")
            db = sqlite3.connect(path)
            db.executescript("""
                CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO settings VALUES ('owner', '42');
                CREATE TABLE reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1
                );
                INSERT INTO reminders(name) VALUES ('Old');
            """)
            db.close()
            db = connect(path)
            try:
                self.assertEqual(db.execute("SELECT user_id FROM reminders").fetchone()[0], 42)
            finally:
                db.close()

    def test_refuses_unowned_legacy_records(self):
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "old.sqlite3")
            db = sqlite3.connect(path)
            db.executescript("""
                CREATE TABLE reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1
                );
                INSERT INTO reminders(name) VALUES ('Old');
            """)
            db.close()
            with self.assertRaisesRegex(ValueError, "no valid owner"):
                connect(path)

    def test_onboarding_invalid_zone_and_persistence(self):
        self.bot.api = lambda method, **kwargs: True
        def message(text):
            self.bot.process_update({"message": {
                "chat": {"id": 77, "type": "private"}, "from": {"id": 77},
                "text": text,
            }}, stamp(2026, 1, 1, 7))
            return self.sent[-1][0][0]

        self.assertIn("IANA time zone", message("/start"))
        self.assertIn("Unknown time zone", message("Not/AZone"))
        self.assertIn("Time zone set to America/New_York", message("America/New_York"))
        self.assertIn("America/New_York", message("/start"))
        self.assertIn("Added reminder", message("/add Pill | 08:00"))
        self.assertIn("America/New_York", message("/timezone"))
        self.assertIn("Europe/London", message("Europe/London"))
        self.assertEqual(self.bot.user_zone(77).key, "Europe/London")

    def test_directory_timezone_during_onboarding_allows_retry(self):
        self.bot.api = lambda method, **kwargs: True
        now = stamp(2026, 1, 1, 7)

        def message(text):
            self.bot.process_update({"message": {
                "chat": {"id": 77, "type": "private"}, "from": {"id": 77},
                "text": text,
            }}, now)

        message("/start")
        # The tzdata fallback can open a directory when given a zone prefix.
        with patch("pill_bot.reminders.ZoneInfo", side_effect=IsADirectoryError):
            message("America/Indiana")
        self.assertIn("Unknown time zone", self.sent[-1][0][0])
        row = self.db.execute("SELECT * FROM users WHERE user_id = 77").fetchone()
        self.assertTrue(row["awaiting_timezone"])
        message("America/Indiana/Indianapolis")
        self.assertEqual(self.bot.user_zone(77).key, "America/Indiana/Indianapolis")
        self.assertFalse(self.db.execute(
            "SELECT awaiting_timezone FROM users WHERE user_id = 77"
        ).fetchone()[0])

    def test_multiple_zones_and_clock_change_preserves_due(self):
        self.bot.set_timezone(99, "America/New_York", 0)
        now = stamp(2026, 1, 1, 7)
        self.bot.add(42, "London", ["08:00"], now)
        self.bot.add(99, "New York", ["08:00"], now)
        self.bot.create_due(stamp(2026, 1, 1, 8))
        self.assertEqual(len(self.doses()), 1)
        self.bot.create_due(stamp(2026, 1, 1, 13))
        self.assertEqual(len(self.doses()), 2)
        self.bot.set_timezone(42, "America/Los_Angeles", stamp(2026, 1, 2, 9))
        self.assertEqual(len(self.doses()), 3)
        self.assertEqual(self.bot.user_zone(99).key, "America/New_York")
        next_due = self.db.execute(
            """SELECT schedules.next_due FROM schedules JOIN reminders
               ON reminders.id = schedules.reminder_id WHERE reminders.user_id = 42"""
        ).fetchone()[0]
        self.assertEqual(datetime.fromtimestamp(next_due, ZoneInfo("America/Los_Angeles")).strftime(
            "%Y-%m-%d %H:%M"), "2026-01-02 08:00")
        self.bot.show_pending(42)
        self.assertIn("PST", self.sent[-1][0][0])

    def test_migrates_global_timezone_once_without_resetting_schedules(self):
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "old.sqlite3")
            db = connect(path)
            db.execute("INSERT INTO settings VALUES ('timezone', 'Europe/London')")
            db.execute("INSERT INTO reminders(user_id, name) VALUES (42, 'Old')")
            db.execute("INSERT INTO schedules(reminder_id, clock, next_due) VALUES (1, '08:00', ?)",
                       (stamp(2026, 1, 1, 8),))
            db.commit()
            bot = ReminderBot("123:token", db)
            bot.bind_database()
            self.assertEqual(bot.user_zone(42).key, "Europe/London")
            self.assertEqual(db.execute("SELECT next_due FROM schedules").fetchone()[0],
                             stamp(2026, 1, 1, 8))
            bot.set_timezone(42, "America/New_York", stamp(2026, 1, 1, 7))
            bot.bind_database()
            self.assertEqual(bot.user_zone(42).key, "America/New_York")
            db.close()

    def test_migration_without_global_timezone_fails(self):
        db = connect(":memory:")
        try:
            db.execute("INSERT INTO reminders(user_id, name) VALUES (42, 'Old')")
            db.commit()
            with self.assertRaisesRegex(ValueError, "no previous time zone"):
                ReminderBot("123:token", db).bind_database()
        finally:
            db.close()

    def test_timezone_setup_survives_restart(self):
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "reminders.sqlite3")
            db = connect(path)
            bot = ReminderBot("123:token", db)
            bot.bind_database()
            bot.command(77, "/start", 0)
            db.close()
            db = connect(path)
            try:
                bot = ReminderBot("123:token", db)
                bot.bind_database()
                replies = []
                bot.send = lambda *args, **kwargs: replies.append(args[0])
                bot.process_update({"message": {
                    "chat": {"id": 77, "type": "private"}, "from": {"id": 77},
                    "text": "America/New_York",
                }}, 0)
                self.assertEqual(bot.user_zone(77).key, "America/New_York")
                self.assertIn("Time zone set", replies[-1])
            finally:
                db.close()

    def test_menu_add_walkthrough_duplicate_and_multiple_times(self):
        now = stamp(2026, 1, 1, 7)
        calls = []
        self.bot.api = lambda method, **kw: calls.append((method, kw)) or True

        def click(action, user=42):
            self.bot.process_update({"callback_query": {
                "id": "click", "from": {"id": user}, "data": action,
                "message": {"chat": {"id": user, "type": "private"}},
            }}, now)

        def message(text, user=42):
            self.bot.process_update({"message": {
                "chat": {"id": user, "type": "private"}, "from": {"id": user}, "text": text,
            }}, now)

        message("/start")
        main_markup = self.sent[-1][0][1]
        self.assertIn("My medications", str(main_markup))
        self.assertNotIn("menu:add", str(main_markup))
        click("menu:list:0")
        self.assertIn("Add medication", str(self.sent[-1][0][1]))
        click("menu:add")
        message("Vitamin D")
        message("bad time")
        self.assertIn("Enter a time", self.sent[-1][0][0])
        message("8:30 am")
        data = self.bot.flow(42)[1]
        click("menu:more:" + data["token"])
        message("20:00")
        click("menu:save:" + data["token"])
        self.assertEqual(self.bot.privacy.decrypt(self.db.execute("SELECT name FROM reminders").fetchone()[0]), "Vitamin D")
        self.assertEqual([r[0] for r in self.db.execute("SELECT clock FROM schedules ORDER BY clock")],
                         ["08:30", "20:00"])
        click("menu:add")
        message("  vitamin d  ")
        self.assertIn("already have", self.sent[-1][0][0])
        self.assertEqual(self.bot.flow(42)[0], "add_name")
        click("menu:home")
        click("menu:add", 99)
        message("vitamin d", 99)
        self.assertEqual(self.bot.flow(99)[0], "time_input")
        self.assertTrue(any(method == "answerCallbackQuery" for method, _ in calls))

    def test_menu_management_and_stale_buttons(self):
        now = stamp(2026, 1, 1, 7)
        reminder = self.bot.add(42, "Old", ["08:00"], now)
        self.bot.api = lambda method, **kw: True
        def click(action, user=42):
            self.bot.process_update({"callback_query": {
                "id": "click", "from": {"id": user}, "data": action,
                "message": {"chat": {"id": user, "type": "private"}},
            }}, now)
        def message(text):
            self.bot.process_update({"message": {
                "chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": text,
            }}, now)

        click(f"menu:rename:{reminder}", 99)
        self.assertIn("not found", self.sent[-1][0][0])
        click(f"menu:rename:{reminder}")
        message("New")
        self.assertEqual(self.bot.privacy.decrypt(self.db.execute("SELECT name FROM reminders").fetchone()[0]), "New")
        click(f"menu:times:{reminder}")
        token = self.bot.flow(42)[1]["token"]
        click("menu:drop:" + token + ":08:00")
        click("menu:more:" + token)
        message("9 pm")
        click("menu:save:" + token)
        self.assertEqual(self.db.execute("SELECT clock FROM schedules").fetchone()[0], "21:00")
        click("menu:save:" + token)
        self.assertIn("no longer active", self.sent[-1][0][0])
        click(f"menu:toggle:{reminder}")
        self.assertEqual(self.db.execute("SELECT enabled FROM reminders").fetchone()[0], 0)
        click(f"menu:toggle:{reminder}")
        self.assertEqual(self.db.execute("SELECT enabled FROM reminders").fetchone()[0], 1)
        click(f"menu:remove:{reminder}")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0], 1)
        click(f"menu:delete:{reminder}", 99)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0], 1)
        click(f"menu:delete:{reminder}")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0], 0)

    def test_menu_callback_edits_pressed_message(self):
        calls = []
        self.bot.api = lambda method, **kw: calls.append((method, kw))
        update = {"callback_query": {
            "id": "click", "from": {"id": 42}, "data": "menu:add",
            "message": {"message_id": 100, "chat": {"id": 42, "type": "private"}},
        }}
        self.bot.process_update(update, stamp(2026, 1, 1, 7))
        self.assertEqual([call[0] for call in calls], ["answerCallbackQuery", "editMessageText"])
        self.assertEqual(calls[1][1]["message_id"], 100)
        self.assertEqual(calls[1][1]["chat_id"], 42)
        self.assertIn("name", calls[1][1]["text"])
        self.assertIn("Cancel", str(calls[1][1]["reply_markup"]))
        self.assertEqual(self.sent, [])
        self.assertIsNone(self.bot.menu_message)

    def test_medication_list_has_add_on_all_pages(self):
        now = stamp(2026, 1, 1, 7)
        for index in range(10):
            self.bot.add(42, f"Pill {index}", ["08:00"], now)
        for page in (0, 1, 2):
            self.bot.show_medications(42, page)
            markup = self.sent[-1][0][1]
            buttons = [button for row in markup["inline_keyboard"] for button in row]
            add = [button for button in buttons if button["callback_data"] == "menu:add"]
            self.assertEqual(len(add), 1)
            self.assertEqual(add[0]["text"], "➕ Add medication")

    def test_edit_times_add_remove_cancel_and_preserve_due(self):
        now = stamp(2026, 1, 1, 7)
        reminder = self.bot.add(42, "Pill", ["08:00", "20:00"], now)
        before = {row["clock"]: (row["id"], row["next_due"]) for row in self.db.execute(
            "SELECT id, clock, next_due FROM schedules WHERE reminder_id = ?", (reminder,)
        )}
        self.bot.menu_action(42, f"times:{reminder}", now)
        state, data = self.bot.flow(42)
        token = data["token"]
        self.assertEqual(state, "time_confirm")
        self.assertEqual(data["times"], ["08:00", "20:00"])
        self.assertIn("Remove 08:00", str(self.sent[-1][0][1]))
        self.bot.menu_action(42, f"more:{token}", now)
        state, data = self.bot.flow(42)
        with self.assertRaisesRegex(ValueError, "already in the list"):
            self.bot.flow_input(42, "8 am", state, data, now)
        self.bot.flow_input(42, "12 pm", state, data, now)
        self.bot.menu_action(42, f"save:{token}", now)
        after = {row["clock"]: (row["id"], row["next_due"]) for row in self.db.execute(
            "SELECT id, clock, next_due FROM schedules WHERE reminder_id = ?", (reminder,)
        )}
        self.assertEqual(after["08:00"], before["08:00"])
        self.assertEqual(after["20:00"], before["20:00"])
        self.assertEqual(after["12:00"][1], stamp(2026, 1, 1, 12))

        self.bot.create_due(stamp(2026, 1, 1, 8))
        self.bot.menu_action(42, f"times:{reminder}", stamp(2026, 1, 1, 9))
        token = self.bot.flow(42)[1]["token"]
        self.bot.menu_action(42, f"drop:{token}:08:00", now)
        self.bot.menu_action(42, "home", now)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM schedules").fetchone()[0], 3)
        self.bot.menu_action(42, f"times:{reminder}", now)
        token = self.bot.flow(42)[1]["token"]
        self.bot.menu_action(42, f"drop:{token}:08:00", now)
        self.bot.menu_action(42, f"save:{token}", now)
        self.assertEqual(len(self.doses()), 1)
        self.assertEqual([row["clock"] for row in self.db.execute(
            "SELECT clock FROM schedules ORDER BY clock")], ["12:00", "20:00"])

    def test_time_editor_requires_a_time_and_rejects_stale_buttons(self):
        now = stamp(2026, 1, 1, 7)
        reminder = self.bot.add(42, "Pill", ["08:00"], now)
        self.bot.menu_action(42, f"times:{reminder}", now)
        token = self.bot.flow(42)[1]["token"]
        self.bot.menu_action(42, f"drop:{token}:08:00", now)
        self.assertNotIn("Save", str(self.sent[-1][0][1]))
        self.bot.menu_action(42, f"save:{token}", now)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM schedules").fetchone()[0], 1)
        self.bot.menu_action(42, f"drop:{token}:08:00", now)
        self.assertIn("no longer", self.sent[-1][0][0])
        self.bot.menu_action(42, "home", now)
        self.bot.menu_action(42, f"times:{reminder}", now)
        new_token = self.bot.flow(42)[1]["token"]
        self.bot.menu_action(42, f"drop:{token}:08:00", now)
        self.assertEqual(self.bot.flow(42)[1]["token"], new_token)
        self.assertEqual(self.bot.flow(42)[1]["times"], ["08:00"])

    def test_edit_time_input_can_return_after_invalid_input(self):
        self.bot.api = lambda method, **kwargs: True
        now = stamp(2026, 1, 1, 7)
        reminder = self.bot.add(42, "Pill", ["08:00"], now)
        self.bot.menu_action(42, f"times:{reminder}", now)
        token = self.bot.flow(42)[1]["token"]
        self.bot.menu_action(42, f"more:{token}", now)
        self.assertIn("Back to times", str(self.sent[-1][0][1]))
        self.assertIn("Enter another time", self.sent[-1][0][0])
        self.bot.process_update({"message": {
            "chat": {"id": 42, "type": "private"}, "from": {"id": 42},
            "text": "invalid",
        }}, now)
        self.assertIn("Back to times", str(self.sent[-1][0][1]))
        self.assertIn("Enter a time", self.sent[-1][0][0])
        self.bot.menu_action(42, f"backtimes:{token}", now)
        self.assertEqual(self.bot.flow(42)[0], "time_confirm")
        self.assertEqual(self.bot.flow(42)[1]["times"], ["08:00"])

    def test_single_message_for_all_text_prompts_and_retries(self):
        now = stamp(2026, 1, 1, 7)
        self.bot.api = lambda method, **kwargs: True
        def message(user, text):
            self.bot.process_update({"message": {
                "chat": {"id": user, "type": "private"}, "from": {"id": user},
                "text": text,
            }}, now)
        def check_prompt(fragment):
            self.assertIn(fragment, self.sent[-1][0][0])
            self.assertIn("Cancel", str(self.sent[-1][0][1]))
            self.assertIn("inline_keyboard", self.sent[-1][0][1])

        message(77, "/start")
        check_prompt("time zone")
        message(77, "Not/AZone")
        check_prompt("Unknown time zone")
        message(77, "Europe/London")
        self.bot.menu_action(42, "timezone", now)
        check_prompt("time zone")
        message(42, "Europe/London")
        self.bot.menu_action(42, "add", now)
        check_prompt("name")
        message(42, "Pill")
        check_prompt("time")
        message(42, "wrong")
        check_prompt("Enter a time")
        message(42, "08:00")
        token = self.bot.flow(42)[1]["token"]
        self.bot.menu_action(42, f"more:{token}", now)
        check_prompt("time")
        message(42, "20:00")
        self.bot.menu_action(42, f"save:{token}", now)
        reminder = self.db.execute(
            "SELECT id FROM reminders WHERE user_id = 42"
        ).fetchone()[0]
        self.bot.menu_action(42, f"rename:{reminder}", now)
        check_prompt("name")
        message(42, "Pill renamed")
        self.bot.menu_action(42, f"times:{reminder}", now)
        token = self.bot.flow(42)[1]["token"]
        self.bot.menu_action(42, f"more:{token}", now)
        check_prompt("time")
        message(42, "bad time")
        check_prompt("Enter a time")

    def test_old_force_reply_cleanup_on_input_cancel_and_retry(self):
        now = stamp(2026, 1, 1, 7)
        calls = []
        def api(method, **kwargs):
            calls.append((method, kwargs))
            return True
        self.bot.api = api
        self.bot.menu_action(42, "add", now)
        prompt = 100
        self.db.execute("INSERT INTO reply_prompts VALUES (?, ?)", (42, prompt))
        self.db.commit()
        self.bot.process_update({"message": {
            "chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": "Pill",
        }}, now)
        self.assertIn(("deleteMessage", {"chat_id": 42, "message_id": prompt}), calls)
        next_prompt = 101
        self.db.execute("INSERT INTO reply_prompts VALUES (?, ?)", (42, next_prompt))
        self.db.commit()
        self.bot.process_update({"callback_query": {
            "id": "cancel", "from": {"id": 42}, "data": "menu:home",
            "message": {"chat": {"id": 42, "type": "private"}},
        }}, now)
        self.assertIn(("deleteMessage", {"chat_id": 42, "message_id": next_prompt}), calls)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reply_prompts").fetchone()[0], 0)
        self.db.execute("INSERT INTO reply_prompts VALUES (?, ?)", (42, 102))
        self.db.commit()
        self.bot.api = lambda method, **kwargs: False
        with self.assertLogs("pill_reminder", level="WARNING"):
            self.bot.clear_reply_prompts(42)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reply_prompts").fetchone()[0], 1)
        self.bot.api = api
        self.bot.clear_reply_prompts(42)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reply_prompts").fetchone()[0], 0)

    def test_menu_edit_rejection_deletes_and_replaces(self):
        calls = []
        def api(method, **kwargs):
            calls.append((method, kwargs))
            if method == "editMessageText":
                raise TelegramResponseError("rejected")
        self.bot.api = api
        update = {"callback_query": {
            "id": "click", "from": {"id": 42}, "data": "menu:add",
            "message": {"message_id": 100, "chat": {"id": 42, "type": "private"}},
        }}
        with self.assertLogs("pill_reminder", level="WARNING"):
            self.bot.process_update(update, stamp(2026, 1, 1, 7))
        self.assertEqual([call[0] for call in calls],
                         ["answerCallbackQuery", "editMessageText", "deleteMessage"])
        self.assertEqual(calls[-1][1], {"chat_id": 42, "message_id": 100})
        self.assertIn("name", self.sent[-1][0][0])
        self.assertEqual(self.sent[-1][1]["chat_id"], 42)

    def test_menu_fallback_logs_delete_failure_and_sends_replacement(self):
        def api(method, **kwargs):
            if method in ("editMessageText", "deleteMessage"):
                raise TelegramResponseError("rejected")
        self.bot.api = api
        self.bot.menu_message = (42, 100)
        with self.assertLogs("pill_reminder", level="WARNING") as logs:
            self.bot.present("New menu", chat_id=42)
        self.assertEqual(len(logs.output), 2)
        self.assertEqual(self.sent[-1][0][0], "New menu")

    def test_menu_network_failure_does_not_delete_message(self):
        calls = []
        def api(method, **kwargs):
            calls.append(method)
            if method == "editMessageText":
                raise TelegramError("network error")
        self.bot.api = api
        self.bot.menu_message = (42, 100)
        with self.assertRaises(TelegramError):
            self.bot.present("New menu", chat_id=42)
        self.assertEqual(calls, ["editMessageText"])
        self.assertEqual(self.sent, [])

    def test_api_timing_logs_method_without_request_data(self):
        self.bot.log_timing = True
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"ok": true, "result": true}'
        with patch.object(self.bot.http, "post", return_value=response):
            with self.assertLogs("pill_reminder", level="INFO") as logs:
                self.bot.api("sendMessage", chat_id=42, text="private medication")
        self.assertIn("Telegram sendMessage took", logs.output[0])
        self.assertNotIn("private medication", logs.output[0])
        self.assertNotIn("123:token", logs.output[0])

    def test_api_reuses_real_http_connection(self):
        connections = []
        requests_seen = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                connections.append(self.client_address)
                requests_seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = b'{"ok": true, "result": true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        real_post = self.bot.http.post
        def local_post(url, **kwargs):
            return real_post(f"http://127.0.0.1:{server.server_port}/test", **kwargs)
        try:
            with patch.object(self.bot.http, "post", side_effect=local_post):
                self.assertTrue(self.bot.api("answerCallbackQuery", callback_query_id="test"))
                self.assertTrue(self.bot.api("editMessageText", text="test"))
            self.assertEqual(len(connections), 2)
            self.assertEqual(connections[0], connections[1])
            self.assertEqual(requests_seen, [{"callback_query_id": "test"}, {"text": "test"}])
        finally:
            self.bot.http.close()
            server.shutdown()
            server.server_close()
            thread.join()

    def test_api_error_closes_session_without_retry(self):
        with patch.object(self.bot.http, "post", side_effect=requests.Timeout("private")) as post:
            with patch.object(self.bot.http, "close") as close:
                with self.assertRaisesRegex(TelegramError, "network or response error") as error:
                    self.bot.api("sendMessage", text="private")
                self.assertNotIn("private", str(error.exception))
                post.assert_called_once()
                close.assert_called_once()

    def test_api_http_error_preserves_rejection_class(self):
        response = requests.Response()
        response.status_code = 400
        response._content = b'{"ok": false}'
        response._content_consumed = True
        with patch.object(self.bot.http, "post", return_value=response):
            with self.assertRaisesRegex(TelegramResponseError, "HTTP 400"):
                self.bot.api("editMessageText")

    def test_register_commands_preserves_existing_commands(self):
        calls = []
        def api(method, **params):
            calls.append((method, params))
            if method == "getMyCommands":
                return [{"command": "help", "description": "Help"}]
            return True
        self.bot.api = api
        self.bot.register_commands()
        self.assertEqual(calls, [
            ("getMyCommands", {}),
            ("setMyCommands", {"commands": [
                {"command": "help", "description": "Help"},
                {"command": "start", "description": "Open the reminder menu"},
                {"command": "stats", "description": "View usage statistics"},
            ]}),
        ])

    def test_register_commands_is_idempotent_and_handles_bad_response(self):
        calls = []
        self.bot.api = lambda method, **kw: calls.append(method) or [
            {"command": "start", "description": "Existing description"},
            {"command": "stats", "description": "Existing stats"},
        ]
        self.bot.register_commands()
        self.assertEqual(calls, ["getMyCommands"])
        self.bot.api = lambda method, **kw: {"command": "start"}
        with self.assertRaisesRegex(TelegramError, "invalid command list"):
            self.bot.register_commands()
        self.bot.api = lambda method, **kw: [] if method == "getMyCommands" else False
        with self.assertRaisesRegex(TelegramError, "not confirmed"):
            self.bot.register_commands()

    def test_register_commands_adds_stats_when_start_already_exists(self):
        calls = []
        def api(method, **params):
            calls.append((method, params))
            if method == "getMyCommands":
                return [{"command": "start", "description": "Existing description"}]
            return True
        self.bot.api = api
        self.bot.register_commands()
        self.assertEqual(calls[-1], ("setMyCommands", {"commands": [
            {"command": "start", "description": "Existing description"},
            {"command": "stats", "description": "View usage statistics"},
        ]}))

    def test_run_registers_at_start_and_survives_registration_failure(self):
        self.bot.register_commands = lambda: (_ for _ in ()).throw(TelegramError("offline"))
        with patch.object(self.bot, "tick", side_effect=KeyboardInterrupt) as tick:
            with self.assertLogs("pill_reminder", level="WARNING") as logs:
                with self.assertRaises(KeyboardInterrupt):
                    self.bot.run()
        self.assertIn("Could not register bot commands", logs.output[0])
        tick.assert_called_once()

    def test_activity_logs_identity_and_no_medication_details(self):
        now = stamp(2026, 1, 1, 7)
        self.bot.process_update({"message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "username": "alice", "first_name": "Alice"},
            "text": "/start",
        }}, now)
        with self.assertLogs("pill_reminder", level="INFO") as logs:
            reminder = self.bot.add(42, "PrivateMedicine", ["08:00"], now)
            self.bot.edit(42, reminder, "PrivateMedicine", ["09:00"], now)
            self.bot.set_enabled(42, reminder, False, now)
            self.bot.set_enabled(42, reminder, True, now)
            self.bot.remove(42, reminder)
        output = "\n".join(logs.output)
        for action in ("added", "edited", "paused", "enabled", "removed"):
            self.assertIn(action + " a medication", output)
        self.assertIn('@alice', output)
        self.assertNotIn("PrivateMedicine", output)
        self.assertNotIn("08:00", output)
        self.assertNotIn("09:00", output)

    def test_activity_log_escapes_identity(self):
        self.bot.remember_identity({"id": 42, "first_name": "Alice\nInjected"})
        with self.assertLogs("pill_reminder", level="INFO") as logs:
            self.bot.log_activity(42, "opened the menu")
        self.assertIn('Alice\\nInjected', "\n".join(logs.output))

    def test_dose_events_are_counted_but_not_logged_after_restart(self):
        now = stamp(2026, 1, 1, 7)
        self.bot.remember_identity({"id": 42, "first_name": "Alice"})
        self.bot.add(42, "PrivateMedicine", ["08:00"], now)
        restarted = ReminderBot("123:token", self.db)
        restarted.send = self.bot.send
        restarted.api = lambda method, **kwargs: None
        due = stamp(2026, 1, 1, 8)
        with self.assertLogs("pill_reminder", level="DEBUG") as logs:
            with patch("pill_reminder.time.time", return_value=due):
                restarted.tick(due)
            dose_id = self.doses()[0]["id"]
            restarted.process_update({"callback_query": {
                "id": "click", "from": {"id": 42}, "data": f"taken:{dose_id}",
                "message": {"message_id": 1, "chat": {"id": 42, "type": "private"}},
            }}, due)
            # assertLogs needs at least one record; nothing else may be logged.
            logging.getLogger("pill_reminder").debug("end of dose events")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.doses()), 0)
        self.assertEqual(logs.output, ["DEBUG:pill_reminder:end of dose events"])
        report = restarted.usage_report(due, user_id=42)
        self.assertIn("was sent a medication reminder: 1", report)
        self.assertIn("marked a dose taken: 1", report)

    def test_failed_or_other_users_actions_do_not_log_success(self):
        now = stamp(2026, 1, 1, 7)
        reminder = self.bot.add(42, "PrivateMedicine", ["08:00"], now)
        with self.assertNoLogs("pill_reminder", level="INFO"):
            self.assertFalse(self.bot.remove(99, reminder))
            self.assertFalse(self.bot.set_enabled(99, reminder, False, now))
            with self.assertRaises(ValueError):
                self.bot.add(42, "privatemedicine", ["08:00"], now)
        self.bot.create_due(stamp(2026, 1, 1, 8))
        def fail(*args, **kwargs):
            raise TelegramError("unavailable")
        self.bot.send = fail
        with self.assertLogs("pill_reminder", level="WARNING") as logs:
            self.bot.tick(stamp(2026, 1, 1, 8))
        self.assertNotIn("was sent a medication reminder", "\n".join(logs.output))

    def test_stats_personal_and_admin_totals_and_retention(self):
        now = 1800000000
        self.bot.admin_id = 123456789
        with self.db:
            self.db.execute("DELETE FROM activity_events")
        self.bot.remember_identity({"id": 42, "first_name": "Old Name"})
        with patch("pill_reminder.time.time", return_value=now):
            self.bot.add(42, "SecretMedicine", ["08:00"], now)
            self.bot.add(99, "OtherSecret", ["09:00"], now)
            self.bot.add(99, "SecondSecret", ["10:00"], now)
        self.bot.remember_identity({"id": 42, "username": "newname"})
        with self.db:
            self.db.execute(
                "INSERT INTO activity_events (user_id, occurred_at, action) VALUES (?, ?, ?)",
                (77, now - ACTIVITY_RETENTION_SECONDS - 1, "old event"),
            )
        def request(user, chat_type="private"):
            self.bot.process_update({"message": {
                "chat": {"id": user, "type": chat_type}, "from": {"id": user},
                "text": "/stats",
            }}, now)

        request(42)
        personal = self.sent[-1][0][0]
        self.assertEqual(personal, "Your usage in the last 90 days:\n  added a medication: 1")
        request(99)
        self.assertEqual(self.sent[-1][0][0],
                         "Your usage in the last 90 days:\n  added a medication: 2")
        request(123456789)
        report = self.sent[-1][0][0]
        self.assertEqual(report, "Active users (last 90 days): 2\n"
                         "Overall usage in the last 90 days:\n  added a medication: 3")
        self.assertNotIn('@newname', report)
        self.assertNotIn("ID 42", report)
        self.assertNotIn("ID 99", report)
        self.assertNotIn("old event", report)
        self.assertNotIn("SecretMedicine", report)
        self.assertNotIn("08:00", report)
        self.bot.prune_activity(now)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM activity_events WHERE action = 'old event'"
        ).fetchone()[0], 0)
        before = len(self.sent)
        request(123456789, "group")
        self.assertEqual(len(self.sent), before)
        self.bot.admin_id = None
        request(123456789)
        self.assertEqual(self.sent[-1][0][0], "Your usage in the last 90 days: no activity recorded.")
        request(42)
        self.assertEqual(self.sent[-1][0][0], personal)

    def test_stats_totals_do_not_grow_with_user_count(self):
        now = 1800000000
        with self.db:
            self.db.execute("DELETE FROM activity_events")
            self.db.executemany(
                "INSERT INTO activity_events (user_id, occurred_at, action) VALUES (?, ?, ?)",
                [(user_id, now, "opened the menu") for user_id in range(1, 2001)],
            )
        report = self.bot.usage_report(now)
        self.assertEqual(report, "Active users (last 90 days): 2000\n"
                         "Overall usage in the last 90 days:\n  opened the menu: 2000")
        self.assertLessEqual(len(report), 4000)

    def test_stats_active_users_empty_and_retention_boundary(self):
        now = 1800000000
        with self.db:
            self.db.execute("DELETE FROM activity_events")
        self.assertEqual(self.bot.usage_report(now),
                         "Active users (last 90 days): 0\n"
                         "Overall usage in the last 90 days: no activity recorded.")
        with self.db:
            self.db.executemany(
                "INSERT INTO activity_events (user_id, occurred_at, action) VALUES (?, ?, ?)",
                [(42, now - ACTIVITY_RETENTION_SECONDS, "opened the menu"),
                 (42, now, "opened the menu"),
                 (99, now - ACTIVITY_RETENTION_SECONDS - 1, "opened the menu")],
            )
        self.assertEqual(self.bot.usage_report(now),
                         "Active users (last 90 days): 1\n"
                         "Overall usage in the last 90 days:\n  opened the menu: 2")
        self.assertNotIn("Active users", self.bot.usage_report(now, user_id=42))

    def test_stats_menu_matches_command_and_preserves_privacy(self):
        now = 1800000000
        self.bot.admin_id = 123456789
        with self.db:
            self.db.execute("DELETE FROM activity_events")
            self.db.executemany(
                "INSERT INTO activity_events (user_id, occurred_at, action) VALUES (?, ?, ?)",
                [(42, now, "opened the menu"), (99, now, "opened the menu"),
                 (99, now, "opened the menu")],
            )
        self.bot.menu(42)
        markup = self.sent[-1][0][1]
        self.assertIn({"text": "📊 Stats", "callback_data": "menu:stats"},
                      [button for row in markup["inline_keyboard"] for button in row])

        calls = []
        self.bot.api = lambda method, **kwargs: calls.append((method, kwargs))
        for user, expected in ((42, 1), (99, 2), (123456789, 3)):
            with self.subTest(user=user):
                self.bot.process_update({"message": {
                    "chat": {"id": user, "type": "private"}, "from": {"id": user},
                    "text": "/stats",
                }}, now)
                command_report = self.sent[-1][0][0]
                self.bot.set_flow(user, "add_name", {})
                calls.clear()
                before = len(self.sent)
                self.bot.process_update({"callback_query": {
                    "id": "click", "from": {"id": user}, "data": "menu:stats",
                    "message": {"message_id": 77, "chat": {"id": user, "type": "private"}},
                }}, now)
                self.assertEqual([call[0] for call in calls],
                                 ["answerCallbackQuery", "editMessageText"])
                edit = calls[-1][1]
                self.assertEqual(edit["chat_id"], user)
                self.assertEqual(edit["message_id"], 77)
                self.assertEqual(edit["text"], command_report)
                self.assertIn(f"opened the menu: {expected}", edit["text"])
                if user == 123456789:
                    self.assertTrue(edit["text"].startswith("Active users (last 90 days): 2\n"))
                else:
                    self.assertNotIn("Active users", edit["text"])
                self.assertIn("menu:home", str(edit["reply_markup"]))
                self.assertEqual(len(self.sent), before)
                self.assertEqual(self.bot.flow(user)[0], None)

        calls.clear()
        self.bot.process_update({"callback_query": {
            "id": "back", "from": {"id": 42}, "data": "menu:home",
            "message": {"message_id": 77, "chat": {"id": 42, "type": "private"}},
        }}, now)
        self.assertEqual(calls[-1][0], "editMessageText")
        self.assertIn("menu:stats", str(calls[-1][1]["reply_markup"]))

    def test_activity_events_persist_across_restart(self):
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "activity.sqlite3")
            db = connect(path)
            bot = ReminderBot("123:token", db)
            bot.bind_database()
            bot.remember_identity({"id": 42, "username": "alice"})
            with patch("pill_reminder.time.time", return_value=1000):
                bot.log_activity(42, "opened the menu")
            db.close()
            db = connect(path)
            try:
                report = ReminderBot("123:token", db).usage_report(1001, user_id=42)
                self.assertNotIn("@alice", report)
                self.assertIn("opened the menu: 1", report)
            finally:
                db.close()

    def test_pending_menu_edits_navigation_but_sends_doses(self):
        now = stamp(2026, 1, 1, 8)
        self.bot.add(42, "Pill", ["08:00"], stamp(2026, 1, 1, 7))
        self.bot.create_due(now)
        calls = []
        self.bot.api = lambda method, **kw: calls.append((method, kw))
        self.bot.process_update({"callback_query": {
            "id": "click", "from": {"id": 42}, "data": "menu:pending",
            "message": {"message_id": 101, "chat": {"id": 42, "type": "private"}},
        }}, now)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Pill", self.sent[0][0][0])
        self.assertEqual(calls[-1][0], "editMessageText")
        self.assertEqual(calls[-1][1]["message_id"], 101)

    def test_clock_input_formats(self):
        for value, expected in (("8 am", "08:00"), ("12:05 AM", "00:05"),
                                ("12 pm", "12:00"), ("9:30 pm", "21:30"),
                                ("20:30", "20:30")):
            with self.subTest(value=value):
                self.assertEqual(parse_clock(value), expected)
        for value in ("13 pm", "8:60 am", "24:00", "8:30"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_clock(value)

    def test_flow_persists_between_bot_instances_and_cancel_discards(self):
        self.bot.menu_action(42, "add", stamp(2026, 1, 1, 7))
        self.bot.flow_input(42, "Aspirin", *self.bot.flow(42), stamp(2026, 1, 1, 7))
        restarted = ReminderBot("123:token", self.db)
        restarted.send = self.bot.send
        self.assertEqual(restarted.flow(42)[0], "time_input")
        restarted.menu_action(42, "home", stamp(2026, 1, 1, 7))
        self.assertEqual(restarted.flow(42)[0], None)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0], 0)

    def test_duplicate_edit_rejected_but_existing_duplicate_schedule_edit_allowed(self):
        now = stamp(2026, 1, 1, 7)
        first = self.bot.add(42, "One", ["08:00"], now)
        second = self.bot.add(42, "Two", ["08:00"], now)
        with self.assertRaisesRegex(ValueError, "already have"):
            self.bot.edit(42, second, "one", ["09:00"], now)
        with self.db:
            self.db.execute("UPDATE reminders SET name = ? WHERE id = ?",
                            (self.bot.privacy.encrypt("One"), second))
        self.assertTrue(self.bot.edit(42, first, "One", ["09:00"], now))
        self.assertEqual(self.db.execute(
            "SELECT clock FROM schedules WHERE reminder_id = ?", (first,)
        ).fetchone()[0], "09:00")

    def test_change_zone_while_disabled_does_not_backfill(self):
        self.bot.add(42, "Pill", ["08:00"], stamp(2026, 1, 1, 7))
        self.bot.create_due(stamp(2026, 1, 1, 8))
        self.bot.set_enabled(42, 1, False, stamp(2026, 1, 1, 9))
        self.bot.set_timezone(42, "America/New_York", stamp(2026, 1, 3, 10))
        self.bot.set_enabled(42, 1, True, stamp(2026, 1, 3, 10))
        self.bot.create_due(stamp(2026, 1, 3, 13))
        self.assertEqual([dose["due"] for dose in self.doses()], [
            stamp(2026, 1, 1, 8), stamp(2026, 1, 3, 13),
        ])


if __name__ == "__main__":
    unittest.main()
