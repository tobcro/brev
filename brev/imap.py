"""IMAP backend built on imaplib. All methods block; call them from a worker thread."""

from __future__ import annotations

import imaplib
import re
import ssl
import time
from email.parser import BytesHeaderParser
from gettext import gettext as _

from . import smtp
from .accounts import xoauth2_bytes
from .models import Folder, MessageSummary, build_folders, summary_from_headers

# Gmail's LIST and SEARCH responses can exceed imaplib's default line limit.
imaplib._MAXLINE = 50_000_000

_SUMMARY_HEADERS = "FROM TO CC REPLY-TO SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES CONTENT-TYPE"
_SUMMARY_ITEMS = f"(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS ({_SUMMARY_HEADERS})])"

_UID_RE = re.compile(rb"UID (\d+)")
_FLAGS_RE = re.compile(rb"FLAGS \(([^)]*)\)")
_SIZE_RE = re.compile(rb"RFC822\.SIZE (\d+)")
_UNSEEN_RE = re.compile(rb"UNSEEN (\d+)")
_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s*(?P<name>.*)$', re.I)


class ImapError(Exception):
    pass


def quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(value: bytes) -> bytes:
    value = value.strip()
    if value.startswith(b'"') and value.endswith(b'"'):
        value = value[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
    return value


def _check(typ, data, what):
    if typ != "OK":
        detail = b" ".join(d for d in data if isinstance(d, bytes)).decode(errors="replace")
        raise ImapError(detail or what)
    return data


def _uid_set(uids) -> str:
    return ",".join(str(u) for u in uids)


def _iter_fetch(data):
    """Yield (metadata, literal-or-None) for each message in a FETCH response."""
    i = 0
    while i < len(data):
        item = data[i]
        if isinstance(item, tuple):
            meta, literal = item[0], item[1]
            if i + 1 < len(data) and isinstance(data[i + 1], bytes) and not _UID_RE.search(data[i + 1]):
                meta += data[i + 1]
                i += 1
            yield meta, literal
        elif isinstance(item, bytes) and _UID_RE.search(item):
            yield item, None
        i += 1


def _flags(meta: bytes) -> set[str]:
    match = _FLAGS_RE.search(meta)
    return set(match.group(1).decode(errors="replace").split()) if match else set()


def _parse_list(data) -> list[Folder]:
    entries = []
    for item in data:
        if item is None:
            continue
        if isinstance(item, tuple):
            line, raw_name = item[0], item[1]
        else:
            line, raw_name = item, None
        match = _LIST_RE.match(line)
        if not match:
            continue
        name = _unquote(raw_name if raw_name is not None else match["name"]).decode("utf-8", "replace")
        delim = None if match["delim"].upper() == b"NIL" else _unquote(match["delim"]).decode()
        flags = {f.lower() for f in match["flags"].decode(errors="replace").split()}
        entries.append((name, delim, flags))
    return build_folders(entries)


class ImapBackend:
    def __init__(self, account):
        self.account = account
        self._conn: imaplib.IMAP4 | None = None
        self._selected: str | None = None
        self._caps: frozenset[str] = frozenset()
        self.uidvalidity: dict[str, int] = {}

    # --- connection ---------------------------------------------------

    def _connect(self):
        cfg = self.account.imap
        context = ssl.create_default_context()
        if cfg.accept_invalid_certs:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        if cfg.security == "ssl":
            conn = imaplib.IMAP4_SSL(cfg.host, cfg.port, ssl_context=context, timeout=20)
        else:
            conn = imaplib.IMAP4(cfg.host, cfg.port, timeout=20)
            if cfg.security == "starttls":
                conn.starttls(ssl_context=context)
        try:
            kind, secret = self.account.credentials("imap")
            if kind == "oauth2":
                token = xoauth2_bytes(cfg.user, secret)
                # A non-empty challenge carries an error; answer it with an empty line.
                conn.authenticate("XOAUTH2", lambda challenge: b"" if challenge else token)
            else:
                conn.login(cfg.user, secret)
            typ, data = conn.capability()
            caps = data[0].decode().upper().split() if typ == "OK" and data and data[0] else conn.capabilities
            self._caps = frozenset(caps)
        except Exception:
            try:
                conn.shutdown()
            except Exception:
                pass
            raise
        self._conn = conn
        self._selected = None

    def _drop(self):
        conn, self._conn, self._selected = self._conn, None, None
        if conn is not None:
            try:
                conn.shutdown()
            except Exception:
                pass

    def close(self):
        if self._conn is not None:
            try:
                self._conn.logout()
            except Exception:
                pass
        self._conn = None
        self._selected = None

    def _run(self, fn):
        """Run fn(conn), reconnecting once if the connection went stale."""
        for attempt in range(2):
            if self._conn is None:
                self._connect()
            try:
                return fn(self._conn)
            except (imaplib.IMAP4.abort, OSError):
                self._drop()
                if attempt:
                    raise

    def _select(self, conn, folder: str):
        if self._selected != folder:
            self._selected = None
            typ, data = conn.select(quote(folder))
            _check(typ, data, f"SELECT {folder}")
            self._selected = folder
            _name, values = conn.response("UIDVALIDITY")
            if values and values[-1] and values[-1].isdigit():
                self.uidvalidity[folder] = int(values[-1])

    def _search(self, conn, *criteria) -> list[int]:
        typ, data = conn.uid("SEARCH", *criteria)
        _check(typ, data, "SEARCH")
        uids = []
        for line in data:
            if isinstance(line, bytes):
                uids.extend(int(x) for x in line.split() if x.isdigit())
        return sorted(set(uids))

    def _fetch_summaries(self, conn, uids) -> list[MessageSummary]:
        summaries = []
        parser = BytesHeaderParser()
        for start in range(0, len(uids), 200):
            batch = uids[start:start + 200]
            typ, data = conn.uid("FETCH", _uid_set(batch), _SUMMARY_ITEMS)
            _check(typ, data, "FETCH")
            for meta, literal in _iter_fetch(data):
                if literal is None:
                    continue
                uid = int(_UID_RE.search(meta).group(1))
                internal = imaplib.Internaldate2tuple(meta)
                fallback = time.mktime(internal) if internal else 0.0
                size = _SIZE_RE.search(meta)
                summaries.append(summary_from_headers(
                    uid, _flags(meta), parser.parsebytes(literal), fallback,
                    int(size.group(1)) if size else 0,
                ))
        summaries.sort(key=lambda s: s.uid, reverse=True)
        return summaries

    # --- folders ------------------------------------------------------

    def list_folders(self, with_counts: bool = True) -> list[Folder]:
        def op(conn):
            data = None
            if "SPECIAL-USE" in self._caps:
                try:
                    typ, data = conn._simple_command("LIST", '""', '"*"', "RETURN", "(SPECIAL-USE)")
                    typ, data = conn._untagged_response(typ, data, "LIST")
                    _check(typ, data, "LIST")
                except imaplib.IMAP4.error:
                    data = None
            if data is None:
                typ, data = conn.list()
                _check(typ, data, "LIST")
            folders = _parse_list(data)
            if not with_counts:
                return folders
            counts = self._unread_counts(conn, [f.name for f in folders if f.counts_unread])
            for folder in folders:
                folder.unread = counts.get(folder.name, 0)
            return folders

        return self._run(op)

    def _unread_counts(self, conn, names) -> dict[str, int]:
        counts = {}
        for name in names:
            typ, data = conn.status(quote(name), "(UNSEEN)")
            if typ != "OK":
                continue
            line = b" ".join(x if isinstance(x, bytes) else b" ".join(x) for x in data if x)
            match = _UNSEEN_RE.search(line)
            if match:
                counts[name] = int(match.group(1))
        return counts

    def unread_counts(self, names) -> dict[str, int]:
        return self._run(lambda conn: self._unread_counts(conn, names))

    # --- messages -----------------------------------------------------

    def list_messages(self, folder: str, limit: int = 50, older_than: int | None = None,
                      unread_only: bool = False):
        """Return (summaries newest first, whether older messages remain)."""
        unseen = ["UNSEEN"] if unread_only else []

        def op(conn):
            self._select(conn, folder)
            if older_than is not None:
                if older_than <= 1:
                    return [], False
                uids = [u for u in self._search(conn, "UID", f"1:{older_than - 1}", *unseen) if u < older_than]
            else:
                uids = self._search(conn, *(unseen or ["ALL"]))
            page = uids[-limit:]
            return self._fetch_summaries(conn, page), len(uids) > len(page)

        return self._run(op)

    def sync(self, folder: str, known_uids):
        """Return (new summaries, {uid: flags} for known messages that still exist)."""
        known = set(known_uids)

        def op(conn):
            self._select(conn, folder)
            conn.noop()
            top = max(known, default=0)
            new = [u for u in self._search(conn, "UID", f"{top + 1}:*") if u > top]
            summaries = self._fetch_summaries(conn, new[-200:])
            flags = {}
            if known:
                typ, data = conn.uid("FETCH", f"{min(known)}:{top}", "(UID FLAGS)")
                _check(typ, data, "FETCH")
                for meta, _literal in _iter_fetch(data):
                    uid = int(_UID_RE.search(meta).group(1))
                    if uid in known:
                        flags[uid] = _flags(meta)
            return summaries, flags

        return self._run(op)

    def new_arrivals(self, folder: str, since: int | None) -> tuple[int, int]:
        """(unread messages that arrived after UID `since`, the folder's highest UID so far).

        since=None just reports the current high-water mark, to start counting from.
        Reads only UIDs; nothing about the messages themselves is fetched.
        """
        def op(conn):
            typ, data = conn.status(quote(folder), "(UIDNEXT)")
            _check(typ, data, "STATUS")
            line = b" ".join(x if isinstance(x, bytes) else b" ".join(x) for x in data if x)
            match = re.search(rb"UIDNEXT (\d+)", line)
            top = int(match.group(1)) - 1 if match else 0
            if since is None or top <= since:
                return 0, max(top, since or 0)
            self._select(conn, folder)
            fresh = [u for u in self._search(conn, "UID", f"{since + 1}:*", "UNSEEN") if u > since]
            return len(fresh), top

        return self._run(op)

    def search(self, folder: str, query: str, limit: int = 100) -> list[MessageSummary]:
        def op(conn):
            self._select(conn, folder)
            if "X-GM-EXT-1" in self._caps:
                conn.literal = query.encode()
                uids = self._search(conn, "CHARSET", "UTF-8", "X-GM-RAW")
            elif query.isascii():
                criteria = []
                for word in query.split():
                    criteria += ["TEXT", quote(word)]
                uids = self._search(conn, *criteria)
            else:
                conn.literal = query.encode()
                uids = self._search(conn, "CHARSET", "UTF-8", "TEXT")
            return self._fetch_summaries(conn, uids[-limit:])

        return self._run(op)

    def fetch_raw(self, folder: str, uid: int) -> bytes:
        def op(conn):
            self._select(conn, folder)
            typ, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
            _check(typ, data, "FETCH")
            for meta, literal in _iter_fetch(data):
                if literal is not None and int(_UID_RE.search(meta).group(1)) == uid:
                    return literal
            raise ImapError(_("The message no longer exists on the server."))

        return self._run(op)

    def fetch_raw_many(self, folder: str, uids) -> dict[int, bytes]:
        def op(conn):
            self._select(conn, folder)
            typ, data = conn.uid("FETCH", _uid_set(uids), "(UID BODY.PEEK[])")
            _check(typ, data, "FETCH")
            return {int(_UID_RE.search(meta).group(1)): literal
                    for meta, literal in _iter_fetch(data) if literal is not None}

        return self._run(op) if uids else {}

    def set_flags(self, folder: str, uids, flag: str, value: bool):
        def op(conn):
            self._select(conn, folder)
            typ, data = conn.uid("STORE", _uid_set(uids), "+FLAGS" if value else "-FLAGS", f"({flag})")
            _check(typ, data, "STORE")

        self._run(op)

    def _expunge(self, conn, uids):
        if "UIDPLUS" in self._caps:
            typ, data = conn.uid("EXPUNGE", _uid_set(uids))
        else:
            typ, data = conn.expunge()
        _check(typ, data, "EXPUNGE")

    def move(self, folder: str, uids, destination: str):
        def op(conn):
            self._select(conn, folder)
            if "MOVE" in self._caps:
                typ, data = conn.uid("MOVE", _uid_set(uids), quote(destination))
                _check(typ, data, "MOVE")
                return
            typ, data = conn.uid("COPY", _uid_set(uids), quote(destination))
            _check(typ, data, "COPY")
            typ, data = conn.uid("STORE", _uid_set(uids), "+FLAGS", "(\\Deleted)")
            _check(typ, data, "STORE")
            self._expunge(conn, uids)

        self._run(op)

    def delete(self, folder: str, uids):
        def op(conn):
            self._select(conn, folder)
            typ, data = conn.uid("STORE", _uid_set(uids), "+FLAGS", "(\\Deleted)")
            _check(typ, data, "STORE")
            self._expunge(conn, uids)

        self._run(op)

    def append(self, folder: str, raw: bytes, flags: str = "(\\Seen)"):
        def op(conn):
            typ, data = conn.append(quote(folder), flags, imaplib.Time2Internaldate(time.time()), raw)
            _check(typ, data, "APPEND")

        self._run(op)

    def send(self, message):
        smtp.send_message(self.account, message)
