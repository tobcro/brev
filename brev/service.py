"""Runs backend calls on a per-account worker thread and reports back on the main loop."""

from __future__ import annotations

import itertools
import logging
import queue
import threading

from gi.repository import GLib

from .message import parse_message

log = logging.getLogger(__name__)

PREFETCH_BATCH = 3

# Job priorities: lower runs first.
URGENT = 0
BACKGROUND = 5
_STOP = 9


def _deliver(callback, value):
    def run():
        callback(value)
        return GLib.SOURCE_REMOVE

    GLib.idle_add(run)


def run_in_thread(fn, done=None, error=None):
    def target():
        try:
            result = fn()
        except Exception as err:
            log.warning("Background task failed", exc_info=True)
            if error:
                _deliver(error, err)
            return
        if done:
            _deliver(done, result)

    threading.Thread(target=target, daemon=True).start()


class Worker:
    """A single thread that runs jobs by priority, so one IMAP connection is never shared."""

    def __init__(self, name: str):
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._order = itertools.count()
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self._thread.start()

    def submit(self, fn, done=None, error=None, priority=URGENT):
        self._queue.put((priority, next(self._order), (fn, done, error)))

    def stop(self):
        self._queue.put((_STOP, next(self._order), None))

    def _loop(self):
        while (job := self._queue.get()[2]) is not None:
            fn, done, error = job
            try:
                result = fn()
            except Exception as err:
                log.info("Task failed: %s", err, exc_info=log.isEnabledFor(logging.DEBUG))
                if error:
                    _deliver(error, err)
                continue
            if done:
                _deliver(done, result)


class MailService:
    """One account: its (caching) backend, its worker and its folders."""

    def __init__(self, account, backend):
        self.account = account
        self.backend = backend
        self.folders = []
        self._prefetch_generation = 0
        self._worker = Worker(f"brev-{account.id}")

    # --- online state (main thread) -----------------------------------

    @property
    def online(self) -> bool:
        return self.backend.online

    @online.setter
    def online(self, value: bool):
        self.backend.online = value

    # --- calls ---------------------------------------------------------

    def call(self, method: str, *args, done=None, error=None, priority=URGENT):
        fn = getattr(self.backend, method)
        self._worker.submit(lambda: fn(*args), done, error, priority)

    def fetch_message(self, folder: str, uid: int, done=None, error=None):
        self._worker.submit(lambda: parse_message(self.backend.fetch_raw(folder, uid)), done, error)

    def prefetch(self, folder: str, summaries, done=None):
        """Download bodies in the background, a few per job.

        Each batch is its own low-priority job, so opening a message or loading
        more of the list never waits for more than one small batch. Starting a
        new prefetch (or cancel_prefetch) drops batches that have not run yet.
        done(count) is called exactly once.
        """
        self._prefetch_generation += 1
        generation = self._prefetch_generation
        wanted = self.backend.prefetch_candidates(folder, summaries)
        batches = [wanted[i:i + PREFETCH_BATCH] for i in range(0, len(wanted), PREFETCH_BATCH)] or [[]]
        fetched = [0]

        def job(batch):
            if generation == self._prefetch_generation:
                fetched[0] += self.backend.prefetch_batch(folder, batch)
            return fetched[0]

        for index, batch in enumerate(batches):
            last = index == len(batches) - 1
            self._worker.submit(lambda b=batch: job(b), done if last else None, done if last else None,
                                BACKGROUND)

    def cancel_prefetch(self):
        self._prefetch_generation += 1

    # Cache reads are quick enough for the main thread.
    def cached_folders(self):
        return self.backend.cached_folders()

    def cached_messages(self, folder, limit, older_than=None, unread_only=False):
        return self.backend.cached_messages(folder, limit, older_than, unread_only)

    def outbox_count(self) -> int:
        return self.backend.outbox_count()

    def folder_for_role(self, role: str):
        return next((f for f in self.folders if f.role == role), None)

    def folder_named(self, name: str):
        return next((f for f in self.folders if f.name == name), None)

    def send(self, message, done=None, error=None):
        """Send on its own thread. done(True) if sent, done(False) if kept in the outbox."""
        sent = self.folder_for_role("sent")

        def finished(delivered):
            if delivered and self.account.saves_sent_copy and sent is not None:
                self.call("append", sent.name, message.as_bytes(), "(\\Seen)")
            if done:
                done(delivered)

        run_in_thread(lambda: self.backend.send(message), finished, error)

    def shutdown(self):
        self._worker.submit(self.backend.close, priority=_STOP - 1)
        self._worker.stop()
