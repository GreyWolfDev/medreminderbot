"""Database initialization, input validation, and shared scheduling helpers."""

import re
import sqlite3
from datetime import datetime, timedelta


ACTIVITY_RETENTION_SECONDS = 90 * 24 * 60 * 60
FORGET_CONFIRM_SECONDS = 5 * 60

TIME_RE = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    timezone TEXT,
    awaiting_timezone INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS user_identities (
    user_id INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activity_events (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    occurred_at INTEGER NOT NULL,
    action TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS activity_events_time ON activity_events(occurred_at);
CREATE TABLE IF NOT EXISTS conversations (
    user_id INTEGER PRIMARY KEY,
    state TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reply_prompts (
    user_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (user_id, message_id)
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_id INTEGER NOT NULL REFERENCES reminders(id) ON DELETE CASCADE,
    clock TEXT NOT NULL,
    next_due INTEGER NOT NULL,
    UNIQUE(reminder_id, clock)
);
CREATE TABLE IF NOT EXISTS doses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_id INTEGER NOT NULL REFERENCES reminders(id) ON DELETE CASCADE,
    due INTEGER NOT NULL,
    next_alert INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS doses_due_alert ON doses(next_alert);
"""

HELP = """Choose an option below. Each scheduled dose is separate and repeats
hourly until you press Taken. Send /start to return to the menu.
Send /forget to delete all your data. If you message the bot again
afterwards, it stores your details again as for a new user."""

TIMEZONE_PROMPT = (
    "Send your IANA time zone, for example Europe/London or America/New_York."
)

def buttons(*rows):
    icons = {
        "menu:add": "➕", "menu:list:": "💊", "menu:pending": "🔔",
        "menu:timezone": "🌍", "menu:stats": "📊", "menu:home": "🏠", "menu:med:": "💊",
        "menu:rename:": "✏️", "menu:times:": "🕒", "menu:toggle:": "⏯️",
        "menu:remove:": "🗑️", "menu:delete:": "🗑️", "menu:drop:": "➖",
        "menu:forget": "🗑️", "menu:save:": "💾", "menu:more:": "➕", "menu:backtimes:": "⬅️",
    }

    def button(label, value):
        if label == "Previous" or label == "Back to medications":
            icon = "⬅️"
        elif label == "Next":
            icon = "➡️"
        elif label == "Cancel" or label == "Keep it":
            icon = "✖️"
        else:
            icon = next((symbol for prefix, symbol in icons.items()
                         if value.startswith(prefix)), "")
        return {"text": f"{icon} {label}" if icon else label, "callback_data": value}

    return {"inline_keyboard": [
        [button(label, value) for label, value in row]
        for row in rows
    ]}

def parse_clock(text):
    value = text.strip().lower()
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", value, re.ASCII)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        if 1 <= hour <= 12 and minute <= 59:
            return f"{hour % 12 + (12 if match.group(3) == 'pm' else 0):02d}:{minute:02d}"
    if TIME_RE.fullmatch(value):
        return value
    raise ValueError("Enter a time like 8:30 am, 8 pm, or 20:30.")

def parse_times(text):
    values = [part.strip() for part in text.split(",")]
    if not values or len(values) > 12 or any(not TIME_RE.fullmatch(v) for v in values):
        raise ValueError("Provide 1 to 12 times in 24-hour format, e.g. 08:00,20:00.")
    if len(set(values)) != len(values):
        raise ValueError("Each time must be unique.")
    return sorted(values)

def parse_name(text):
    name = text.strip()
    if not name or len(name) > 100 or "\n" in name:
        raise ValueError("The name must be 1 to 100 characters on one line.")
    return name

def parse_id(text):
    value = text.strip()
    if not value.isascii() or not value.isdigit() or len(value) > 19:
        raise ValueError("ID must be a positive integer.")
    number = int(value)
    if not 0 < number <= 9223372036854775807:
        raise ValueError("ID must be a positive integer.")
    return number

def occurrence(day, clock, zone):
    hour, minute = map(int, clock.split(":"))
    local = datetime(day.year, day.month, day.day, hour, minute)
    # Pick the first occurrence in autumn; move forward through a spring gap.
    while True:
        stamp = int(local.replace(tzinfo=zone, fold=0).timestamp())
        if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) == local:
            return stamp
        local += timedelta(minutes=1)

def next_occurrence(clock, after, zone):
    day = datetime.fromtimestamp(after, zone).date()
    result = occurrence(day, clock, zone)
    if result <= after:
        result = occurrence(day + timedelta(days=1), clock, zone)
    return result

def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    columns = {row["name"] for row in db.execute("PRAGMA table_info(reminders)")}
    if "user_id" not in columns:
        owner = db.execute("SELECT value FROM settings WHERE key = 'owner'").fetchone()
        has_reminders = db.execute("SELECT 1 FROM reminders LIMIT 1").fetchone()
        if has_reminders and (not owner or not owner["value"].isascii()
                              or not owner["value"].isdigit() or int(owner["value"]) == 0):
            db.close()
            raise ValueError("Cannot migrate reminders: the old database has no valid owner.")
        with db:
            db.execute("ALTER TABLE reminders ADD COLUMN user_id INTEGER")
            if owner:
                db.execute("UPDATE reminders SET user_id = ?", (int(owner["value"]),))
    return db
