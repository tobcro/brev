"""Cache-aware backend: keeps the local cache current and queues changes while offline."""

from __future__ import annotations

import base64
import imaplib
import logging
import ssl
from email import policy
from email.parser import BytesParser
from gettext import gettext as _

from .accounts import AccountError

log = logging.getLogger(__name__)

# Bodies larger than this are not downloaded ahead of time.
PREFETCH_MAX_BYTES = 3 * 1024 * 1024


class OfflineError(Exception):
    def __init__(self, message=None):
        super().__init__(message or _("You are offline."))


def is_connectivity_error(err: Exception) -> bool:
    if isinstance(err, ssl.SSLCertVerificationError):
        return False  # a real problem, not a missing network
    return isinstance(err, (OSError, imaplib.IMAP4.abort, OfflineError))


class CachingBackend:
    """Same interface as ImapBackend, with the cache in front of it.

    Runs on the account's worker thread. `online` is set from the main thread.
    """

    def __init__(self, account, remote, cache):
        self.account = account
        self.remote = remote
        self.cache = cache
        self.online = True

    @property
    def _id(self):
        return self.account.id

    def _call(self, method, *args):
        if not self.online:
            raise OfflineError()
        try:
            return getattr(self.remote, method)(*args)
        except AccountError:
            raise
        except Exception as err:
            if is_connectivity_error(err):
                raise OfflineError() from err
            raise

    # --- folders --------------------------------------------------------

    def cached_folders(self):
        return self.cache.load_folders(self._id)

    def list_folders(self, with_counts=True):
        folders = self._call("list_folders", with_counts)
        if not with_counts:
            # Keep previously known counts until fresh ones arrive.
            known = {f.name: f.unread for f in self.cache.load_folders(self._id)}
            for folder in folders:
                folder.unread = known.get(folder.name, 0)
        self.cache.save_folders(self._id, folders)
        return folders

    def unread_counts(self, names):
        counts = self._call("unread_counts", names)
        self.cache.save_counts(self._id, counts)
        return counts

    # --- messages -------------------------------------------------------

    def cached_messages(self, folder, limit=50, older_than=None, unread_only=False):
        return self.cache.load_summaries(self._id, folder, limit, older_than, unread_only)

    def _remember_validity(self, folder):
        validity = getattr(self.remote, "uidvalidity", {}).get(folder)
        self.cache.check_uidvalidity(self._id, folder, validity)

    def list_messages(self, folder, limit=50, older_than=None, unread_only=False):
        summaries, has_more = self._call("list_messages", folder, limit, older_than, unread_only)
        self._remember_validity(folder)
        if unread_only:
            pass  # a partial view: read messages are absent, not deleted
        elif older_than is None and summaries:
            # Anything cached in the newest-page range that the server no longer lists is gone.
            cached, _more = self.cache.load_summaries(self._id, folder, limit * 4)
            live = {s.uid for s in summaries}
            lowest = min(live)
            self.cache.remove_messages(self._id, folder, [s.uid for s in cached
                                                          if s.uid >= lowest and s.uid not in live])
        elif older_than is None:
            self.cache.clear_folder(self._id, folder)
        self.cache.save_summaries(self._id, folder, summaries)
        return summaries, has_more

    def sync(self, folder, known_uids):
        new, flags = self._call("sync", folder, known_uids)
        self._remember_validity(folder)
        self.cache.save_summaries(self._id, folder, new)
        self.cache.update_flags(self._id, folder, flags)
        self.cache.remove_messages(self._id, folder, [u for u in known_uids if u not in flags])
        return new, flags

    def new_arrivals(self, folder, since):
        return self._call("new_arrivals", folder, since)

    def search(self, folder, query, limit=100):
        try:
            return self._call("search", folder, query, limit)
        except OfflineError:
            return self.cache.search(self._id, folder, query, limit)

    def fetch_raw(self, folder, uid):
        raw = self.cache.load_body(self._id, folder, uid)
        if raw is not None:
            return raw
        try:
            raw = self._call("fetch_raw", folder, uid)
        except OfflineError as err:
            raise OfflineError(_("This message has not been saved for offline reading.")) from err
        self.cache.save_body(self._id, folder, uid, raw)
        return raw

    def prefetch_candidates(self, folder, summaries) -> list[int]:
        """UIDs worth downloading ahead of time (not cached yet, not huge). Cache read only."""
        have = self.cache.cached_body_uids(self._id, folder)
        return [s.uid for s in summaries if s.uid not in have and s.size <= PREFETCH_MAX_BYTES]

    def prefetch_batch(self, folder, uids) -> int:
        """Download a few bodies so they can be read offline. Best effort; never raises."""
        if not self.online or not uids:
            return 0
        try:
            bodies = self._call("fetch_raw_many", folder, list(uids))
        except Exception:
            log.info("Prefetch batch failed", exc_info=True)
            return 0
        for uid, raw in bodies.items():
            self.cache.save_body(self._id, folder, uid, raw)
        return len(bodies)

    # --- changes (queued while offline) -------------------------------

    def _change(self, method, *args):
        try:
            self._call(method, *args)
            return True
        except OfflineError:
            self.cache.add_pending(self._id, method, list(args))
            return False

    def set_flags(self, folder, uids, flag, value):
        self.cache.change_flag(self._id, folder, list(uids), flag, value)
        return self._change("set_flags", folder, list(uids), flag, value)

    def move(self, folder, uids, destination):
        self.cache.remove_messages(self._id, folder, uids)
        return self._change("move", folder, list(uids), destination)

    def delete(self, folder, uids):
        self.cache.remove_messages(self._id, folder, uids)
        return self._change("delete", folder, list(uids))

    def append(self, folder, raw, flags="(\\Seen)"):
        try:
            self._call("append", folder, raw, flags)
        except OfflineError:
            self.cache.add_pending(self._id, "append", [folder, base64.b64encode(raw).decode(), flags])

    def send(self, message):
        """Send now, or keep in the outbox. Returns True if it went out."""
        try:
            self._call("send", message)
            return True
        except OfflineError:
            self.cache.add_outbox(self._id, message.as_bytes())
            return False

    def outbox_count(self):
        return self.cache.outbox_count(self._id)

    def flush(self):
        """Replay queued changes and send queued mail. Returns (changes, sent, errors)."""
        changes = sent = 0
        errors = []
        for op_id, method, args in self.cache.pending(self._id):
            if method == "append":
                args = [args[0], base64.b64decode(args[1]), args[2]]
            try:
                self._call(method, *args)
            except OfflineError:
                return changes, sent, errors
            except Exception as err:  # e.g. the message is gone; nothing to retry
                log.warning("Dropping queued %s: %s", method, err)
            self.cache.remove_pending(op_id)
            changes += 1
        for item_id, raw in self.cache.outbox(self._id):
            message = BytesParser(policy=policy.default).parsebytes(raw)
            try:
                self._call("send", message)
            except OfflineError:
                break
            except Exception as err:
                errors.append(err)
                continue
            self.cache.remove_outbox(item_id)
            sent += 1
            if self.account.saves_sent_copy:
                sent_folder = next((f for f in self.cache.load_folders(self._id) if f.role == "sent"), None)
                if sent_folder:
                    self.append(sent_folder.name, raw)
        return changes, sent, errors

    def close(self):
        self.remote.close()
