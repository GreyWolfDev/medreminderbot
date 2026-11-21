"""Telegram API and message presentation."""

import logging
import time

import requests

from .common import buttons

LOG = logging.getLogger("pill_reminder")


class TelegramError(Exception):
    pass


class TelegramResponseError(TelegramError):
    """Telegram answered but refused the request.

    status is the HTTP status; error_code, description and retry_after come
    from Telegram's reply when it included them, and are None otherwise.
    """

    def __init__(self, message, *, status=None, error_code=None, description=None,
                 retry_after=None):
        super().__init__(message)
        self.status = status
        self.error_code = error_code
        self.description = description
        self.retry_after = retry_after


def _int_field(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def rejection(method, status, data):
    if not isinstance(data, dict):
        data = {}
    description = data.get("description")
    if isinstance(description, str):
        description = " ".join(description.split())[:200] or None
    else:
        description = None
    parameters = data.get("parameters")
    retry_after = _int_field(parameters.get("retry_after")) if isinstance(parameters, dict) else None
    if retry_after is not None and retry_after < 0:
        retry_after = None
    if 200 <= status < 300:
        message = f"{method}: Telegram rejected the request"
    else:
        message = f"{method}: HTTP {status}"
    if description:
        message += f": {description}"
    return TelegramResponseError(message, status=status, error_code=_int_field(data.get("error_code")),
                                 description=description, retry_after=retry_after)


class TelegramMixin:
    def api(self, method, **params):
        started = time.monotonic()
        try:
            try:
                with self.http.post(
                    "https://api.telegram.org/bot" + self.token + "/" + method,
                    json=params, timeout=20, allow_redirects=False,
                ) as response:
                    status = response.status_code
                    if not 200 <= status < 300:
                        try:
                            body = response.json()
                        except ValueError:
                            body = None
                        raise rejection(method, status, body)
                    data = response.json()
            except (requests.RequestException, ValueError):
                self.http.close()
                raise TelegramError(f"{method}: network or response error") from None
        finally:
            if self.log_timing:
                LOG.info("Telegram %s took %.3f seconds", method, time.monotonic() - started)
        if not isinstance(data, dict) or not data.get("ok"):
            raise rejection(method, status, data)
        return data["result"]

    def register_commands(self):
        commands = self.api("getMyCommands")
        if (not isinstance(commands, list) or
                any(not isinstance(item, dict) or not isinstance(item.get("command"), str)
                    or not isinstance(item.get("description"), str) for item in commands)):
            raise TelegramError("getMyCommands: invalid command list")
        existing = {item["command"] for item in commands}
        additions = [
            {"command": "start", "description": "Open the reminder menu"},
            {"command": "stats", "description": "View usage statistics"},
        ]
        missing = [item for item in additions if item["command"] not in existing]
        if not missing:
            return
        commands.extend(missing)
        if self.api("setMyCommands", commands=commands) is not True:
            raise TelegramError("setMyCommands: registration was not confirmed")
        LOG.info("Registered bot commands with Telegram.")

    def send(self, text, markup=None, *, chat_id):
        if markup is not None and len(text) > 4000:
            raise ValueError("Button message is too long.")
        result = None
        for start in range(0, len(text), 4000):
            params = {"chat_id": chat_id,
                      "text": text[start:start + 4000]}
            if markup is not None:
                params["reply_markup"] = markup
            result = self.api("sendMessage", **params)
        return result

    def present(self, text, markup=None, *, chat_id):
        target = self.menu_message
        self.menu_message = None
        if target is None or target[0] != chat_id:
            self.send(text, markup, chat_id=chat_id)
            return
        params = {"chat_id": chat_id, "message_id": target[1], "text": text,
                  "reply_markup": markup if markup is not None else {"inline_keyboard": []}}
        try:
            self.api("editMessageText", **params)
        except TelegramResponseError as exc:
            LOG.warning("Could not edit menu message: %s; replacing it", exc)
            try:
                self.api("deleteMessage", chat_id=chat_id, message_id=target[1])
            except TelegramError as delete_error:
                LOG.warning("Could not delete old menu message: %s", delete_error)
            self.send(text, markup, chat_id=chat_id)

    def request_text(self, user_id, text, controls=None):
        if controls is None:
            controls = buttons([("Cancel", "menu:home")])
        self.present(text, controls, chat_id=user_id)

    def clear_reply_prompts(self, user_id):
        prompts = self.db.execute(
            "SELECT message_id FROM reply_prompts WHERE user_id = ?", (user_id,)
        ).fetchall()
        for prompt in prompts:
            try:
                result = self.api("deleteMessage", chat_id=user_id, message_id=prompt["message_id"])
                if result is not True:
                    raise TelegramError("deleteMessage: deletion was not confirmed")
            except TelegramError as exc:
                LOG.warning("Could not remove reply prompt for user %s: %s", user_id, exc)
            else:
                with self.db:
                    self.db.execute(
                        "DELETE FROM reply_prompts WHERE user_id = ? AND message_id = ?",
                        (user_id, prompt["message_id"]),
                    )
