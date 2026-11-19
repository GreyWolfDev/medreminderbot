"""Text commands, menu screens, and guided medication flows."""

import json
import re
import secrets

from .common import HELP, buttons, parse_clock, parse_id, parse_name, parse_times


class MenuMixin:
    def command(self, user_id, text, now):
        command, _, args = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        args = args.strip()
        if command == "/timezone":
            self.clear_flow(user_id)
            if args:
                return self.set_timezone(user_id, args, now)
            row = self.db.execute("SELECT timezone FROM users WHERE user_id = ?", (user_id,)).fetchone()
            prefix = f"Current time zone: {row['timezone']}\n" if row and row["timezone"] else ""
            return prefix + self.prompt_timezone(user_id)
        if command == "/start":
            row = self.db.execute("SELECT timezone FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if not row or not row["timezone"]:
                return self.prompt_timezone(user_id)
            return f"Your time zone: {row['timezone']}\n\n{HELP}"
        if command == "/help":
            return HELP
        if command in ("/add", "/edit", "/enable", "/disable"):
            row = self.db.execute("SELECT timezone FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if not row or not row["timezone"]:
                return self.prompt_timezone(user_id)
        if command == "/list":
            self.log_activity(user_id, "viewed their medications")
            rows = self.db.execute(
                """SELECT reminders.id, reminders.name, reminders.enabled,
                          GROUP_CONCAT(schedules.clock, ', ') AS clocks
                   FROM reminders LEFT JOIN schedules ON schedules.reminder_id = reminders.id
                   WHERE reminders.user_id = ?
                   GROUP BY reminders.id ORDER BY reminders.id""", (user_id,)
            ).fetchall()
            if not rows:
                return "No reminders. Use /add to create one."
            return "\n".join(f"{r['id']}: {self.privacy.decrypt(r['name'])} — {r['clocks']} "
                             f"({'enabled' if r['enabled'] else 'paused'})" for r in rows)
        if command == "/pending":
            return None
        if command == "/add":
            parts = args.split("|")
            if len(parts) != 2:
                raise ValueError("Use /add Name | 08:00,20:00")
            reminder_id = self.add(user_id, parse_name(parts[0]), parse_times(parts[1]), now)
            return f"Added reminder {reminder_id}."
        if command == "/edit":
            parts = args.split("|")
            if len(parts) != 3:
                raise ValueError("Use /edit ID | Name | 08:00,20:00")
            reminder_id = parse_id(parts[0])
            found = self.edit(user_id, reminder_id, parse_name(parts[1]), parse_times(parts[2]), now)
            return f"Updated reminder {reminder_id}." if found else "Reminder not found."
        if command in ("/disable", "/enable", "/remove"):
            reminder_id = parse_id(args)
            if command == "/remove":
                found = self.remove(user_id, reminder_id)
                return "Reminder and pending doses removed." if found else "Reminder not found."
            found = self.set_enabled(user_id, reminder_id, command == "/enable", now)
            return ("Reminder enabled." if command == "/enable" else "Reminder paused.") if found else "Reminder not found."
        return "Unknown command. Send /help for instructions."

    def show_pending(self, user_id, *, empty_notice=True):
        self.log_activity(user_id, "viewed pending doses")
        rows = self.db.execute(
            """SELECT doses.id, doses.due, reminders.name, reminders.enabled, users.timezone
               FROM doses JOIN reminders ON reminders.id = doses.reminder_id
               JOIN users ON users.user_id = reminders.user_id
               WHERE reminders.user_id = ? ORDER BY doses.due, doses.id""", (user_id,)
        ).fetchall()
        if not rows and empty_notice:
            self.send("No pending doses.", chat_id=user_id)
        for row in rows:
            text, markup = self.dose_message(row)
            if not row["enabled"]:
                text += "\nPaused"
            self.send(text, markup, chat_id=user_id)
        return bool(rows)

    def flow(self, user_id):
        row = self.db.execute(
            "SELECT state, data FROM conversations WHERE user_id = ?", (user_id,)
        ).fetchone()
        return (row["state"], json.loads(self.privacy.decrypt(row["data"]))) if row else (None, {})

    def set_flow(self, user_id, state, data):
        if "token" not in data:
            data["token"] = secrets.token_hex(4)
        with self.db:
            self.db.execute(
                """INSERT INTO conversations VALUES (?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET state = excluded.state, data = excluded.data""",
                (user_id, state, self.privacy.encrypt(json.dumps(data))),
            )
            self.db.execute("UPDATE users SET awaiting_timezone = 0 WHERE user_id = ?", (user_id,))

    def clear_flow(self, user_id):
        with self.db:
            self.db.execute("DELETE FROM conversations WHERE user_id = ?", (user_id,))
            self.db.execute("UPDATE users SET awaiting_timezone = 0 WHERE user_id = ?", (user_id,))

    def menu(self, user_id, text=HELP):
        self.present(text, buttons(
            [("My medications", "menu:list:0")],
            [("Pending doses", "menu:pending"), ("Time zone", "menu:timezone")],
            [("Stats", "menu:stats")],
        ), chat_id=user_id)

    def show_medications(self, user_id, page):
        self.log_activity(user_id, "viewed their medications")
        rows = self.db.execute(
            """SELECT id, name, enabled FROM reminders WHERE user_id = ?
               ORDER BY id LIMIT 9 OFFSET ?""", (user_id, page * 8)
        ).fetchall()
        choices = [[(self.privacy.decrypt(r["name"]) + (" (paused)" if not r["enabled"] else ""),
                     f"menu:med:{r['id']}")] for r in rows[:8]]
        navigation = []
        if page:
            navigation.append(("Previous", f"menu:list:{page - 1}"))
        if len(rows) > 8:
            navigation.append(("Next", f"menu:list:{page + 1}"))
        if navigation:
            choices.append(navigation)
        choices.append([("Add medication", "menu:add")])
        choices.append([("Main menu", "menu:home")])
        self.present("Choose a medication." if rows else "No medications on this page.",
                  buttons(*choices), chat_id=user_id)

    def show_medication(self, user_id, reminder_id):
        row = self.db.execute(
            "SELECT name, enabled FROM reminders WHERE id = ? AND user_id = ?",
            (reminder_id, user_id),
        ).fetchone()
        if not row:
            self.menu(user_id, "Medication not found.")
            return
        clocks = [slot["clock"] for slot in self.db.execute(
            "SELECT clock FROM schedules WHERE reminder_id = ? ORDER BY clock", (reminder_id,)
        )]
        status = "active" if row["enabled"] else "paused"
        self.present(f"{self.privacy.decrypt(row['name'])}\nTimes: {', '.join(clocks)}\nStatus: {status}", buttons(
            [("Change name", f"menu:rename:{reminder_id}"),
             ("Change times", f"menu:times:{reminder_id}")],
            [("Pause" if row["enabled"] else "Enable", f"menu:toggle:{reminder_id}")],
            [("Remove", f"menu:remove:{reminder_id}")],
            [("Back to medications", "menu:list:0"), ("Main menu", "menu:home")],
        ), chat_id=user_id)

    def flow_input(self, user_id, text, state, data, now):
        if state in ("add_name", "rename"):
            name = parse_name(text)
            reminder_id = data.get("id")
            self.check_duplicate(user_id, name, reminder_id)
            if state == "rename":
                row = self.db.execute(
                    "SELECT id FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, user_id)
                ).fetchone()
                if not row:
                    self.clear_flow(user_id)
                    self.menu(user_id, "Medication not found.")
                    return
                with self.db:
                    self.db.execute("UPDATE reminders SET name = ? WHERE id = ?",
                                    (self.privacy.encrypt(name), reminder_id))
                    self.db.execute("DELETE FROM conversations WHERE user_id = ?", (user_id,))
                self.log_activity(user_id, "edited a medication")
                self.show_medication(user_id, reminder_id)
            else:
                self.set_flow(user_id, "time_input", {"name": name, "times": [], "mode": "add"})
                self.request_text(user_id, "What time do you take it? For example, 8:30 am or 20:30.",
                                  buttons([("Cancel", "menu:home")]))
        elif state == "time_input":
            clock = parse_clock(text)
            if clock in data["times"]:
                raise ValueError("That time is already in the list. Enter a different time.")
            data["times"].append(clock)
            self.set_flow(user_id, "time_confirm", data)
            self.show_time_confirmation(user_id, data)
        elif state == "time_confirm":
            self.show_time_confirmation(user_id, data)

    def show_time_confirmation(self, user_id, data):
        if data["mode"] == "edit":
            rows = [[(f"Remove {clock}", f"menu:drop:{data['token']}:{clock}")]
                    for clock in sorted(data["times"])]
            controls = []
            if len(data["times"]) < 12:
                controls.append(("Add a time", f"menu:more:{data['token']}"))
            if data["times"]:
                controls.append(("Save", f"menu:save:{data['token']}"))
            if controls:
                rows.append(controls)
            rows.append([("Cancel", "menu:home")])
            times = ", ".join(sorted(data["times"])) or "none"
            self.present(f"Daily times: {times}\nAdd or remove times, then save. "
                         "At least one time is required.", buttons(*rows), chat_id=user_id)
            return
        rows = [[("Save", f"menu:save:{data['token']}")]]
        if len(data["times"]) < 12:
            rows[0].append(("Add another time", f"menu:more:{data['token']}"))
        rows.append([("Cancel", "menu:home")])
        self.present(f"Daily times: {', '.join(sorted(data['times']))}", buttons(*rows),
                  chat_id=user_id)

    def show_time_input(self, user_id, data):
        controls = [[("Cancel", "menu:home")]]
        if data["mode"] == "edit":
            controls.insert(0, [("Back to times", f"menu:backtimes:{data['token']}")])
        self.request_text(user_id, "Enter another time, for example 8:30 am or 20:30.",
                          buttons(*controls))

    def menu_action(self, user_id, action, now):
        if action == "home":
            self.clear_flow(user_id)
            self.menu(user_id)
        elif action == "add":
            try:
                self.user_zone(user_id)
            except ValueError:
                self.clear_flow(user_id)
                self.request_text(user_id, self.prompt_timezone(user_id),
                                  buttons([("Cancel", "menu:home")]))
                return
            self.set_flow(user_id, "add_name", {})
            self.request_text(user_id, "What is the medication's name?",
                              buttons([("Cancel", "menu:home")]))
        elif action == "pending":
            self.clear_flow(user_id)
            has_pending = self.show_pending(user_id, empty_notice=False)
            self.menu(user_id, "Return to the menu." if has_pending else "No pending doses.")
        elif action == "timezone":
            self.clear_flow(user_id)
            self.request_text(user_id, self.command(user_id, "/timezone", now),
                              buttons([("Cancel", "menu:home")]))
        elif action == "stats":
            self.clear_flow(user_id)
            self.present(self.usage_report_for(user_id, now),
                         buttons([("Main menu", "menu:home")]), chat_id=user_id)
        elif action.startswith("list:"):
            self.clear_flow(user_id)
            page = parse_id(action[5:]) if action[5:] != "0" else 0
            self.show_medications(user_id, page)
        elif re.fullmatch(r"(?:more|save|backtimes):[0-9a-f]{8}|drop:[0-9a-f]{8}:[0-2][0-9]:[0-5][0-9]", action):
            operation, token, *parts = action.split(":", 2)
            state, data = self.flow(user_id)
            if operation == "backtimes" and state == "time_input" and data.get("token") == token and data["mode"] == "edit":
                self.set_flow(user_id, "time_confirm", data)
                self.show_time_confirmation(user_id, data)
            elif state != "time_confirm" or data.get("token") != token:
                self.menu(user_id, "That step is no longer active.")
            elif operation == "drop":
                clock = parts[0]
                if data["mode"] != "edit" or clock not in data["times"]:
                    self.present("That time is no longer in the list.",
                                 buttons([("Main menu", "menu:home")]), chat_id=user_id)
                else:
                    data["times"].remove(clock)
                    self.set_flow(user_id, "time_confirm", data)
                    self.show_time_confirmation(user_id, data)
            elif operation == "more" and len(data["times"]) < 12:
                self.set_flow(user_id, "time_input", data)
                self.show_time_input(user_id, data)
            elif operation == "save":
                if not data["times"]:
                    self.show_time_confirmation(user_id, data)
                    return
                if data["mode"] == "add":
                    self.add(user_id, data["name"], sorted(data["times"]), now, finish_flow=True)
                    self.menu(user_id, "Medication added.")
                else:
                    row = self.db.execute(
                        "SELECT name FROM reminders WHERE id = ? AND user_id = ?",
                        (data["id"], user_id),
                    ).fetchone()
                    if not row:
                        self.clear_flow(user_id)
                        self.menu(user_id, "Medication not found.")
                    else:
                        self.edit(user_id, data["id"], self.privacy.decrypt(row["name"]), sorted(data["times"]), now,
                                  finish_flow=True)
                        self.show_medication(user_id, data["id"])
            else:
                self.show_time_confirmation(user_id, data)
        else:
            match = re.fullmatch(r"(med|rename|times|toggle|remove|delete):([0-9]{1,19})", action)
            if not match:
                self.menu(user_id, "That button is not available.")
                return
            operation, number = match.groups()
            reminder_id = parse_id(number)
            row = self.db.execute(
                "SELECT name, enabled FROM reminders WHERE id = ? AND user_id = ?",
                (reminder_id, user_id),
            ).fetchone()
            if not row:
                self.menu(user_id, "Medication not found.")
                return
            self.clear_flow(user_id)
            if operation == "med":
                self.show_medication(user_id, reminder_id)
            elif operation == "rename":
                self.set_flow(user_id, "rename", {"id": reminder_id})
                self.request_text(user_id, "Enter the new medication name.",
                                  buttons([("Cancel", "menu:home")]))
            elif operation == "times":
                clocks = [slot["clock"] for slot in self.db.execute(
                    "SELECT clock FROM schedules WHERE reminder_id = ? ORDER BY clock",
                    (reminder_id,),
                )]
                data = {"id": reminder_id, "times": clocks, "mode": "edit"}
                self.set_flow(user_id, "time_confirm", data)
                self.show_time_confirmation(user_id, data)
            elif operation == "toggle":
                self.create_due(now)
                self.set_enabled(user_id, reminder_id, not row["enabled"], now)
                self.show_medication(user_id, reminder_id)
            elif operation == "remove":
                self.present(f"Remove {self.privacy.decrypt(row['name'])} and all its pending doses?", buttons(
                    [("Yes, remove", f"menu:delete:{reminder_id}")],
                    [("Keep it", f"menu:med:{reminder_id}")],
                ), chat_id=user_id)
            else:
                if self.remove(user_id, reminder_id):
                    self.menu(user_id, "Medication and pending doses removed.")
                else:
                    self.menu(user_id, "Medication not found.")
