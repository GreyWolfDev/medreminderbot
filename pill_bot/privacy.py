"""Authenticated encryption for medication names and conversation state."""

from cryptography.fernet import Fernet, InvalidToken


class EncryptionError(RuntimeError):
    pass


class Privacy:
    def __init__(self, key):
        if not key:
            raise EncryptionError("Set PILL_ENCRYPTION_KEY to a persistent Fernet key.")
        try:
            self.fernet = Fernet(key.encode("ascii"))
        except (ValueError, UnicodeEncodeError):
            raise EncryptionError("PILL_ENCRYPTION_KEY is not a valid Fernet key.") from None

    def encrypt(self, value):
        return self.fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value):
        try:
            return self.fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, AttributeError):
            raise EncryptionError("Cannot decrypt database data; check the encryption key and database integrity.") from None

    def initialize(self, db):
        db.execute("PRAGMA secure_delete = ON")
        marker = db.execute(
            "SELECT value FROM settings WHERE key = 'encryption_check'"
        ).fetchone()
        if marker:
            if self.decrypt(marker["value"]) != "pill-reminder-encryption-v1":
                raise EncryptionError("Cannot decrypt database data; check the encryption key and database integrity.")
        else:
            with db:
                for row in db.execute("SELECT id, name FROM reminders").fetchall():
                    db.execute("UPDATE reminders SET name = ? WHERE id = ?",
                               (self.encrypt(row["name"]), row["id"]))
                for row in db.execute("SELECT user_id, data FROM conversations").fetchall():
                    db.execute("UPDATE conversations SET data = ? WHERE user_id = ?",
                               (self.encrypt(row["data"]), row["user_id"]))
                db.execute("INSERT INTO settings VALUES ('encryption_check', ?)",
                           (self.encrypt("pill-reminder-encryption-v1"),))
                db.execute("INSERT INTO settings VALUES ('encryption_cleanup_pending', '1')")

        pending = db.execute(
            "SELECT 1 FROM settings WHERE key = 'encryption_cleanup_pending'"
        ).fetchone()
        if pending:
            self._checkpoint(db)
            db.execute("VACUUM")
            self._checkpoint(db)
            with db:
                db.execute("DELETE FROM settings WHERE key = 'encryption_cleanup_pending'")
            self._checkpoint(db)

    @staticmethod
    def _checkpoint(db):
        result = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result and result[0] != 0:
            raise EncryptionError("Cannot remove old database WAL contents; stop other database connections and retry.")
