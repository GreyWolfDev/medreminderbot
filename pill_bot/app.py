"""Coordinate Telegram updates and the reminder service loop."""

import logging
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from .activity import ActivityMixin
from .common import HELP, buttons, parse_id
from .menus import MenuMixin
from .privacy import Privacy
from .reminders import ReminderMixin
from .telegram import TelegramError, TelegramMixin

LOG = logging.getLogger("pill_reminder")


class ReminderBot(ActivityMixin, TelegramMixin, ReminderMixin, MenuMixin):
    def __init__(self, token, db, admin_id=None):
        self.token = token
        self.db = db
        self.privacy = Privacy(os.environ.get("PILL_ENCRYPTION_KEY"))
        self.admin_id = admin_id
        self.menu_message = None
        self.log_timing = os.environ.get("PILL_LOG_TIMING") == "1"
        self.http = requests.Session()
        self.flood_wait_until = 0
        self.send_backoff = {}

    def process_update(self, update, now):
        message = update.get("message", {})
        callback = update.get("callback_query")
        if callback:
            chat = callback.get("message", {}).get("chat", {})
            user = callback.get("from", {}).get("id")
            authorized = isinstance(user, int) and user > 0 and user == chat.get("id") and chat.get("type") == "private"
            answer = "Not authorized."
            confirmation = None
            if authorized:
                self.remember_identity(callback["from"])
                self.mark_reachable(user, now)
                action = callback.get("data", "")
                if action.startswith("menu:"):
                    self.api("answerCallbackQuery", callback_query_id=callback["id"])
                    self.clear_reply_prompts(user)
                    message_id = callback.get("message", {}).get("message_id")
                    self.menu_message = (user, message_id) if isinstance(message_id, int) else None
                    try:
                        self.menu_action(user, action[5:], now)
                    except ValueError as exc:
                        self.present(str(exc), buttons([("Main menu", "menu:home")]), chat_id=user)
                    finally:
                        self.menu_message = None
                    return
                match = re.fullmatch(r"taken:([0-9]+)", action)
                if match:
                    try:
                        dose_id = parse_id(match.group(1))
                    except ValueError:
                        answer = "Unknown action."
                    else:
                        with self.db:
                            dose = self.db.execute(
                                """SELECT doses.due, reminders.name, users.timezone
                                   FROM doses JOIN reminders ON reminders.id = doses.reminder_id
                                   JOIN users ON users.user_id = reminders.user_id
                                   WHERE doses.id = ? AND reminders.user_id = ?""",
                                (dose_id, user),
                            ).fetchone()
                            deleted = self.db.execute(
                                """DELETE FROM doses WHERE id = ? AND reminder_id IN
                                   (SELECT id FROM reminders WHERE user_id = ?)""",
                                (dose_id, user),
                            ).rowcount
                        answer = "Marked taken." if deleted else "Already taken or removed."
                        if deleted:
                            self.log_activity(user, "marked a dose taken")
                            zone = ZoneInfo(dose["timezone"])
                            taken = datetime.fromtimestamp(now, zone)
                            original = callback["message"].get("text")
                            if not isinstance(original, str) or not original:
                                scheduled = datetime.fromtimestamp(dose["due"], zone)
                                original = (f"Medication reminder: {self.privacy.decrypt(dose['name'])}\n"
                                            f"Scheduled: {scheduled:%Y-%m-%d %H:%M %Z}")
                            confirmation = f"{original}\nTaken: {taken:%Y-%m-%d %H:%M:%S %Z}"
                else:
                    answer = "Unknown action."
            try:
                if confirmation is not None:
                    message_id = callback["message"].get("message_id")
                    if not isinstance(message_id, int):
                        raise TelegramError("Cannot edit taken reminder: missing message ID")
                    self.api("editMessageText", chat_id=user, message_id=message_id,
                             text=confirmation, reply_markup={"inline_keyboard": []})
            finally:
                self.api("answerCallbackQuery", callback_query_id=callback["id"], text=answer)
            return
        chat = message.get("chat", {})
        user = message.get("from", {}).get("id")
        if not isinstance(user, int) or user <= 0 or chat.get("type") != "private" or user != chat.get("id"):
            return
        self.remember_identity(message["from"])
        self.mark_reachable(user, now)
        text = message.get("text", "").strip()
        if text.split("@", 1)[0].split(" ", 1)[0].lower() == "/stats":
            self.send(self.usage_report_for(user, now), chat_id=user)
            return
        if text.split("@", 1)[0] in ("/start", "/help"):
            self.log_activity(user, "opened the menu")
            self.clear_reply_prompts(user)
            self.clear_flow(user)
            row = self.db.execute("SELECT timezone FROM users WHERE user_id = ?", (user,)).fetchone()
            if not row or not row["timezone"]:
                self.request_text(user, self.prompt_timezone(user))
            else:
                self.menu(user, f"Your time zone: {row['timezone']}\n\n{HELP}")
            return
        if text.split("@", 1)[0] == "/id":
            self.send(f"Your Telegram user ID: {user}", chat_id=user)
            return
        try:
            row = self.db.execute(
                "SELECT awaiting_timezone FROM users WHERE user_id = ?", (user,)
            ).fetchone()
            if row and row["awaiting_timezone"] and text and not text.startswith("/"):
                self.clear_reply_prompts(user)
                response = self.set_timezone(user, text, now)
                self.menu(user, response)
                return
            else:
                state, data = self.flow(user)
                if state and text and not text.startswith("/"):
                    self.clear_reply_prompts(user)
                    self.flow_input(user, text, state, data, now)
                    return
                response = self.command(user, text, now)
        except ValueError as exc:
            response = str(exc)
            state, data = self.flow(user)
            if state in ("add_name", "rename", "time_input"):
                controls = [[("Cancel", "menu:home")]]
                if state == "time_input" and data.get("mode") == "edit":
                    controls.insert(0, [("Back to times", f"menu:backtimes:{data['token']}")])
                self.request_text(user, response, buttons(*controls))
                return
            row = self.db.execute(
                "SELECT awaiting_timezone FROM users WHERE user_id = ?", (user,)
            ).fetchone()
            if row and row["awaiting_timezone"]:
                self.request_text(user, response)
                return
        if response is None:
            self.show_pending(user)
        else:
            row = self.db.execute(
                "SELECT awaiting_timezone FROM users WHERE user_id = ?", (user,)
            ).fetchone()
            if row and row["awaiting_timezone"] and text.split(" ", 1)[0].split("@", 1)[0] == "/timezone":
                self.request_text(user, response)
            else:
                self.send(response, chat_id=user)

    def run(self):
        stored = self.db.execute("SELECT value FROM settings WHERE key = 'offset'").fetchone()
        offset = int(stored["value"]) if stored else 0
        next_prune = 0
        next_command_registration = 0
        commands_registered = False
        while True:
            now = int(time.time())
            if not commands_registered and now >= next_command_registration:
                try:
                    self.register_commands()
                    commands_registered = True
                except TelegramError as exc:
                    LOG.warning("Could not register bot commands: %s; retrying in five minutes", exc)
                    next_command_registration = now + 300
            if now >= next_prune:
                self.prune_activity(now)
                next_prune = now + 86400
            self.tick(int(time.time()))
            try:
                updates = self.api("getUpdates", offset=offset, timeout=5,
                                   allowed_updates=["message", "callback_query"])
                for update in updates:
                    started = time.monotonic()
                    try:
                        self.create_due(int(time.time()))
                        self.process_update(update, int(time.time()))
                    except TelegramError as exc:
                        LOG.warning("Could not respond to update: %s", exc)
                    finally:
                        if self.log_timing:
                            kind = "button" if "callback_query" in update else "message"
                            LOG.info("Processing %s update took %.3f seconds",
                                     kind, time.monotonic() - started)
                    offset = update["update_id"] + 1
                    with self.db:
                        self.db.execute("INSERT OR REPLACE INTO settings VALUES ('offset', ?)",
                                        (str(offset),))
            except TelegramError as exc:
                LOG.warning("Telegram polling failed: %s", exc)
                time.sleep(5)
