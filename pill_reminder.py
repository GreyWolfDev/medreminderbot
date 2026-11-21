"""Multi-user Telegram pill reminders with a persistent SQLite database."""

import logging
import os
import re
import signal
import time as time

from pill_bot.app import ReminderBot
from pill_bot.common import (ACTIVITY_RETENTION_SECONDS, HELP, SCHEMA, TIME_RE,
                             TIMEZONE_PROMPT, buttons, connect, next_occurrence,
                             occurrence, parse_clock, parse_id, parse_name,
                             parse_times)
from pill_bot.telegram import TelegramError, TelegramResponseError
from pill_bot.privacy import EncryptionError

LOG = logging.getLogger(__name__)

__all__ = [
    "ACTIVITY_RETENTION_SECONDS", "HELP", "SCHEMA", "TIME_RE", "TIMEZONE_PROMPT",
    "ReminderBot", "TelegramError", "TelegramResponseError", "buttons", "connect",
    "main", "next_occurrence", "occurrence", "parse_clock", "parse_id",
    "parse_name", "parse_times",
]


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise SystemExit("Set BOT_TOKEN to your Telegram bot token.")
    admin_text = os.environ.get("PILL_ADMIN_ID", "").strip()
    try:
        admin_id = parse_id(admin_text) if admin_text else None
    except ValueError:
        raise SystemExit("PILL_ADMIN_ID must be a positive numeric Telegram ID.") from None
    os.umask(0o077)
    try:
        db = connect(os.environ.get("PILL_DB", "pill_reminders.sqlite3"))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    try:
        bot = ReminderBot(token, db, admin_id)
    except EncryptionError as exc:
        db.close()
        raise SystemExit(str(exc)) from None
    def request_stop(signum, frame):
        # systemd stops services with SIGTERM; finish the current step, then exit.
        bot.stopping = True

    previous_sigterm = signal.signal(signal.SIGTERM, request_stop)
    try:
        bot.bind_database()
        LOG.info("Multi-user reminder bot started.")
        bot.run()
        LOG.info("Bot stopped.")
    except (ValueError, EncryptionError) as exc:
        raise SystemExit(str(exc)) from None
    except KeyboardInterrupt:
        LOG.info("Bot stopped.")
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
        bot.http.close()
        db.close()


if __name__ == "__main__":
    main()
