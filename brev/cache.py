"""On-disk cache of folders, message summaries, bodies and offline changes (SQLite).

One connection shared by all threads, guarded by a lock. Message data is purged
down to the last few days on exit; queued changes and unsent mail are kept.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time

from gi.repository import GLib

from .models import Address, Folder, MessageSummary

log = logging.getLogger(__name__)

KEEP_DAYS = 7

_SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    account TEXT, name TEXT, path TEXT, display_name TEXT, delimiter TEXT, role TEXT,
    depth INTEGER, unread INTEGER, position INTEGER, uidvalidity INTEGER,
    PRIMARY KEY (account, name));
CREATE TABLE IF NOT EXISTS messages (
    account TEXT, folder TEXT, uid INTEGER, timestamp REAL, flags TEXT, data TEXT,
    PRIMARY KEY (account, folder, uid));
CREATE INDEX IF NOT EXISTS messages_time ON messages (timestamp);
CREATE TABLE IF NOT EXISTS bodies (
    account TEXT, folder TEXT, uid INTEGER, raw BLOB,
    PRIMARY KEY (account, folder, uid));
CREATE TABLE IF NOT EXISTS pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT, op TEXT, args TEXT, created REAL);
CREATE TABLE IF NOT EXISTS contacts (
    source TEXT, uid TEXT, data TEXT, PRIMARY KEY (source, uid));
CREATE TABLE IF NOT EXISTS remote_images (
    account TEXT, message_id TEXT, PRIMARY KEY (account, message_id));
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT, raw BLOB, created REAL);
"""


def default_path(demo=False) -> str:
    folder = os.path.join(GLib.get_user_cache_dir(), "brev")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "demo-cache.db" if demo else "cache.db")


def _addr(a: Address | None):
    return [a.name, a.email] if a else None


def _addrs(items):
    return [_addr(a) for a in items]


def _from_addr(value):
    return Address(value[0], value[1]) if value else None


def _encode(s: MessageSummary) -> str:
    return json.dumps({
        "subject": s.subject, "sender": _addr(s.sender), "to": _addrs(s.to), "cc": _addrs(s.cc),
        "reply_to": _addrs(s.reply_to), "message_id": s.message_id, "in_reply_to": s.in_reply_to,
        "references": s.references, "size": s.size, "has_attachments": s.has_attachments,
    })


def _decode(uid, timestamp, flags, data) -> MessageSummary:
    d = json.loads(data)
    return MessageSummary(
        uid=uid, flags=set(json.loads(flags)), timestamp=timestamp, subject=d["subject"],
        sender=_from_addr(d["sender"]), to=[_from_addr(a) for a in d["to"]],
        cc=[_from_addr(a) for a in d["cc"]], reply_to=[_from_addr(a) for a in d["reply_to"]],
        message_id=d["message_id"], in_reply_to=d["in_reply_to"], references=d["references"],
        size=d["size"], has_attachments=d["has_attachments"],
    )


class Cache:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)

    def _q(self, sql, args=()):
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _many(self, sql, rows):
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.executemany(sql, rows)
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    # --- folders --------------------------------------------------------

    def save_folders(self, account: str, folders: list[Folder]):
        validity = dict(self._q("SELECT name, uidvalidity FROM folders WHERE account=?", (account,)))
        with self._lock:
            self._db.execute("BEGIN")
            self._db.execute("DELETE FROM folders WHERE account=?", (account,))
            self._db.executemany(
                "INSERT INTO folders VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(account, f.name, f.path, f.display_name, f.delimiter, f.role, f.depth, f.unread, i,
                  validity.get(f.name)) for i, f in enumerate(folders)])
            names = [f.name for f in folders]
            marks = ",".join("?" * len(names)) or "''"
            for table in ("messages", "bodies"):
                self._db.execute(f"DELETE FROM {table} WHERE account=? AND folder NOT IN ({marks})",
                                 (account, *names))
            self._db.execute("COMMIT")

    def load_folders(self, account: str) -> list[Folder]:
        rows = self._q("SELECT name, path, display_name, delimiter, role, depth, unread FROM folders "
                       "WHERE account=? ORDER BY position", (account,))
        return [Folder(*row) for row in rows]

    def save_counts(self, account: str, counts: dict[str, int]):
        self._many("UPDATE folders SET unread=? WHERE account=? AND name=?",
                   [(n, account, name) for name, n in counts.items()])

    def check_uidvalidity(self, account: str, folder: str, value: int | None):
        """Forget a folder's messages if the server renumbered it."""
        if value is None:
            return
        row = self._q("SELECT uidvalidity FROM folders WHERE account=? AND name=?", (account, folder))
        if row and row[0][0] is not None and row[0][0] != value:
            log.info("UIDVALIDITY changed for %s; dropping cached messages", folder)
            self.clear_folder(account, folder)
        self._q("UPDATE folders SET uidvalidity=? WHERE account=? AND name=?", (value, account, folder))

    def clear_folder(self, account: str, folder: str):
        for table in ("messages", "bodies"):
            self._q(f"DELETE FROM {table} WHERE account=? AND folder=?", (account, folder))

    # --- messages -------------------------------------------------------

    def save_summaries(self, account: str, folder: str, summaries):
        self._many("INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?)",
                   [(account, folder, s.uid, s.timestamp, json.dumps(sorted(s.flags)), _encode(s))
                    for s in summaries])

    def load_summaries(self, account: str, folder: str, limit: int, older_than: int | None = None,
                       unread_only: bool = False):
        bound = older_than if older_than is not None else 2 ** 62
        unread = " AND instr(flags, 'Seen') = 0" if unread_only else ""
        rows = self._q("SELECT uid, timestamp, flags, data FROM messages WHERE account=? AND folder=? "
                       f"AND uid<?{unread} ORDER BY uid DESC LIMIT ?", (account, folder, bound, limit + 1))
        return [_decode(*row) for row in rows[:limit]], len(rows) > limit

    def update_flags(self, account: str, folder: str, flags_by_uid: dict[int, set]):
        self._many("UPDATE messages SET flags=? WHERE account=? AND folder=? AND uid=?",
                   [(json.dumps(sorted(f)), account, folder, uid) for uid, f in flags_by_uid.items()])

    def change_flag(self, account: str, folder: str, uids, flag: str, value: bool):
        rows = self._q(f"SELECT uid, flags FROM messages WHERE account=? AND folder=? "
                       f"AND uid IN ({','.join('?' * len(uids))})", (account, folder, *uids))
        updates = {}
        for uid, flags in rows:
            current = set(json.loads(flags))
            (current.add if value else current.discard)(flag)
            updates[uid] = current
        self.update_flags(account, folder, updates)

    def remove_messages(self, account: str, folder: str, uids):
        uids = list(uids)
        for table in ("messages", "bodies"):
            self._many(f"DELETE FROM {table} WHERE account=? AND folder=? AND uid=?",
                       [(account, folder, uid) for uid in uids])

    def save_body(self, account: str, folder: str, uid: int, raw: bytes):
        self._q("INSERT OR REPLACE INTO bodies VALUES (?,?,?,?)", (account, folder, uid, raw))

    def load_body(self, account: str, folder: str, uid: int) -> bytes | None:
        row = self._q("SELECT raw FROM bodies WHERE account=? AND folder=? AND uid=?", (account, folder, uid))
        return row[0][0] if row else None

    def cached_body_uids(self, account: str, folder: str) -> set[int]:
        return {r[0] for r in self._q("SELECT uid FROM bodies WHERE account=? AND folder=?", (account, folder))}

    def search(self, account: str, folder: str, query: str, limit: int):
        sql = ("SELECT m.uid, m.timestamp, m.flags, m.data FROM messages m LEFT JOIN bodies b "
               "ON b.account=m.account AND b.folder=m.folder AND b.uid=m.uid "
               "WHERE m.account=? AND m.folder=?")
        args = [account, folder]
        for word in query.split():
            sql += " AND (m.data LIKE ? OR CAST(b.raw AS TEXT) LIKE ?)"
            args += [f"%{word}%", f"%{word}%"]
        sql += " ORDER BY m.uid DESC LIMIT ?"
        args.append(limit)
        return [_decode(*row) for row in self._q(sql, args)]

    # --- offline changes ----------------------------------------------

    def add_pending(self, account: str, op: str, args: list):
        self._q("INSERT INTO pending (account, op, args, created) VALUES (?,?,?,?)",
                (account, op, json.dumps(args), time.time()))

    def pending(self, account: str):
        return [(row[0], row[1], json.loads(row[2]))
                for row in self._q("SELECT id, op, args FROM pending WHERE account=? ORDER BY id", (account,))]

    def remove_pending(self, op_id: int):
        self._q("DELETE FROM pending WHERE id=?", (op_id,))

    def add_outbox(self, account: str, raw: bytes):
        self._q("INSERT INTO outbox (account, raw, created) VALUES (?,?,?)", (account, raw, time.time()))

    def outbox(self, account: str):
        return self._q("SELECT id, raw FROM outbox WHERE account=? ORDER BY id", (account,))

    def outbox_count(self, account: str) -> int:
        return self._q("SELECT COUNT(*) FROM outbox WHERE account=?", (account,))[0][0]

    def remove_outbox(self, item_id: int):
        self._q("DELETE FROM outbox WHERE id=?", (item_id,))

    # --- "Show Images" choices ---------------------------------------
    # Remembered per email (Message-ID, so Gmail's copies in other folders share it), and
    # only while that email is in the cache: purging the email forgets the choice too.

    def is_cached(self, account: str, folder: str, uid: int) -> bool:
        return bool(self._q("SELECT 1 FROM messages WHERE account=? AND folder=? AND uid=?",
                            (account, folder, uid)))

    def allow_remote_images(self, account: str, message_id: str):
        self._q("INSERT OR IGNORE INTO remote_images VALUES (?,?)", (account, message_id))

    def remote_images_allowed(self, account: str, message_id: str) -> bool:
        return bool(self._q("SELECT 1 FROM remote_images WHERE account=? AND message_id=?",
                            (account, message_id)))

    # --- contacts (not purged: they are not mail) ----------------------

    def replace_contacts(self, source: str, contacts: list[dict]):
        with self._lock:
            self._db.execute("BEGIN")
            self._db.execute("DELETE FROM contacts WHERE source=?", (source,))
            self._db.executemany("INSERT OR REPLACE INTO contacts VALUES (?,?,?)",
                                 [(source, c["uid"], json.dumps(c)) for c in contacts])
            self._db.execute("COMMIT")

    def load_contacts(self) -> list[dict]:
        return [json.loads(row[0]) for row in self._q("SELECT data FROM contacts")]

    def forget_contact_sources(self, keep: list[str]):
        marks = ",".join("?" * len(keep)) or "''"
        self._q(f"DELETE FROM contacts WHERE source NOT IN ({marks})", tuple(keep))

    # --- housekeeping -------------------------------------------------

    def purge(self, keep_days: int = KEEP_DAYS, accounts=None):
        """Drop messages older than keep_days (and data for removed accounts).

        Queued changes and unsent mail are always kept.
        """
        cutoff = time.time() - keep_days * 24 * 3600
        with self._lock:
            self._db.execute("BEGIN")
            if accounts is not None:
                marks = ",".join("?" * len(accounts)) or "''"
                for table in ("folders", "messages", "bodies", "remote_images"):
                    self._db.execute(f"DELETE FROM {table} WHERE account NOT IN ({marks})", tuple(accounts))
            self._db.execute("DELETE FROM messages WHERE timestamp < ?", (cutoff,))
            self._db.execute("DELETE FROM bodies WHERE NOT EXISTS (SELECT 1 FROM messages m WHERE "
                             "m.account=bodies.account AND m.folder=bodies.folder AND m.uid=bodies.uid)")
            self._db.execute("DELETE FROM remote_images WHERE NOT EXISTS (SELECT 1 FROM messages m WHERE "
                             "m.account=remote_images.account "
                             "AND json_extract(m.data, '$.message_id')=remote_images.message_id)")
            self._db.execute("COMMIT")
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._db.execute("VACUUM")

    def stats(self) -> dict:
        return {t: self._q(f"SELECT COUNT(*) FROM {t}")[0][0]
                for t in ("folders", "messages", "bodies", "pending", "outbox", "contacts", "remote_images")}

    def close(self):
        with self._lock:
            self._db.close()
