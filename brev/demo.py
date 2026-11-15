"""An offline account with sample mail, for trying the UI and taking screenshots.

Start with `bin/brev --demo`. Nothing here touches the network.
"""

from __future__ import annotations

import os
import time
from email.message import EmailMessage
from email.parser import BytesHeaderParser
from email.utils import formatdate, make_msgid

from .models import build_folders, summary_from_headers


class DemoAccount:
    id = "demo"
    provider = "demo"
    provider_name = "Demo"
    email = "sam@example.org"
    identity = email
    display_name = "Sam Rivera"
    title = email
    saves_sent_copy = True
    signature = ("demo",)

    def credentials(self, service):
        raise RuntimeError("The demo account has no credentials")


_HOUR = 3600
_DAY = 24 * _HOUR

_NEWSLETTER = """<html><head><style>p{color:#3d3846;line-height:1.5}</style></head><body>
<table width="100%" cellpadding="0" cellspacing="0" style="background:#1c71d8"><tr>
<td style="padding:16px"><img src="cid:banner@demo" width="320" alt="Waves" style="display:block;border-radius:8px"></td></tr></table>
<h1 style="font-family:Georgia,serif;color:#1a5fb4">The Low Tide Digest</h1>
<img src="https://example.org/track.gif" width="1" height="1" alt="">
<p>This week on the coast: <b>spring tides</b>, a new <a href="https://example.org/birds">bird survey</a>
and the long-awaited return of the harbour market.</p>
<h2>Upcoming</h2>
<ul><li>Saturday — beach clean-up at the north pier</li>
<li>Sunday — guided walk along the tideline, 10:00</li>
<li>Wednesday — talk: <i>Why terns travel so far</i></li></ul>
<blockquote>“The Arctic tern sees two summers every year.”</blockquote>
<p>See you on the shore,<br>The Digest team</p>
<p><a href="https://example.org/unsubscribe">Unsubscribe</a></p>
</body></html>"""

_SKETCH = b"""<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200">
<rect width="320" height="200" fill="#99c1f1"/><path d="M0 150 Q80 120 160 150 T320 150 V200 H0Z" fill="#1c71d8"/>
<path d="M120 80 l40 20 l40 -20" stroke="#241f31" stroke-width="5" fill="none" stroke-linecap="round"/></svg>"""

# (folder, age, from, to, subject, body, flags, extra)
_RECEIPT = """<html><body style="font-family:Helvetica,Arial,sans-serif;background:#f6f5f4">
<div style="background:#2ec27e;color:white;padding:24px;font-size:22px;font-weight:bold">Order confirmed ✓</div>
<div style="padding:16px"><p>Thanks for your order, Sam! Here is your receipt.</p>
<table style="width:100%;border-collapse:collapse">
<tr style="border-bottom:1px solid #ccc"><td>Rain jacket, size M</td><td align="right">849 kr</td></tr>
<tr style="border-bottom:1px solid #ccc"><td>Wool socks ×2</td><td align="right">198 kr</td></tr>
<tr><td><b>Total</b></td><td align="right"><b>1 047 kr</b></td></tr></table>
<p style="color:#77767b">Order number 2026-11-0042</p></div></body></html>"""

_SAMPLES = [
    ("INBOX", 0.3 * _HOUR, ("Maja Lindqvist", "maja@example.net"), None,
     "Lunch on Friday?",
     "Hi Sam,\n\nAre you free for lunch on Friday? The new place by the harbour opened last week "
     "and I've heard good things about their fish soup.\n\nMenu: https://example.net/harbour-kitchen\n\n"
     "Maja", set(), None),
    ("INBOX", 2 * _HOUR, ("Jonas Berg", "jonas@example.com"), None,
     "Re: Release notes for 0.4",
     "Looks great — I fixed the typo in the second paragraph and added the screenshot.\n\n"
     "One question: should we mention the new keyboard shortcuts in the summary, or keep them in the "
     "full changelog only?\n\nOn Tue, Jonas wrote earlier:\n> Draft is in the shared folder.\n"
     "> Please have a look before Thursday.\n>\n> Thanks!",
     set(), "attachment"),
    ("INBOX", 5 * _HOUR, ("The Low Tide Digest", "digest@example.org"), None,
     "Spring tides, a bird survey and the harbour market", None, set(), "html"),
    ("INBOX", 26 * _HOUR, ("Priya Natarajan", "priya@example.com"), None,
     "Photos from the trip",
     "Here are a few photos from the weekend! The light at the lighthouse was unreal.\n\n"
     "I'll upload the rest to the shared album tonight.\n\nPriya", {"\\Seen", "\\Flagged"}, None),
    ("INBOX", 2 * _DAY, ("Calendar", "calendar@example.org"), None,
     "Reminder: Dentist appointment tomorrow at 09:30",
     "This is a reminder for your appointment tomorrow at 09:30.\n\nNeed to reschedule? "
     "Reply to this message or call the clinic.", {"\\Seen"}, None),
    ("INBOX", 3 * _DAY, ("Erik Holm", "erik@example.net"), None,
     "Board game night 🎲",
     "We're doing board games at my place on the 20th. Bring snacks if you can!\n\n— Erik",
     {"\\Seen"}, None),
    ("INBOX", 4 * _DAY, ("Harbour Outdoor Shop", "orders@shop.example"), None,
     "Your order is confirmed", None, set(), "receipt"),
    ("INBOX", 9 * _DAY, ("Bank Notifications", "no-reply@bank.example"), None,
     "Your monthly statement is ready",
     "Your statement for last month is now available in the app.\n\nThis is an automated message.",
     {"\\Seen"}, None),
    ("INBOX", 40 * _DAY, ("Ana Sousa", "ana@example.pt"), None,
     "Thanks for the recommendation",
     "Just finished the book you recommended — couldn't put it down. Any more like it?\n\nAna",
     {"\\Seen"}, None),
    ("[Mail]/Sent", 1 * _DAY, ("Sam Rivera", "sam@example.org"), ("Maja Lindqvist", "maja@example.net"),
     "Notes from the meeting",
     "Hi Maja,\n\nHere are my notes from today. Let me know if I missed anything.\n\nSam",
     {"\\Seen"}, None),
    ("[Mail]/Drafts", 4 * _HOUR, ("Sam Rivera", "sam@example.org"), ("Jonas Berg", "jonas@example.com"),
     "Ideas for the next release", "- Faster search\n- Offline mode\n- ", {"\\Seen", "\\Draft"}, None),
    ("Projects/Tideline", 6 * _DAY, ("Jonas Berg", "jonas@example.com"), None,
     "Tideline settings mock-ups",
     "Attached are the new settings mock-ups. I kept the layout close to the GNOME HIG.", set(), None),
    ("Archive", 60 * _DAY, ("Travel Desk", "travel@example.com"), None,
     "Your booking confirmation", "Your booking is confirmed. Have a good trip!", {"\\Seen"}, None),
]


def _build_message(age, sender, to, subject, body, extra):
    msg = EmailMessage()
    msg["From"] = f"{sender[0]} <{sender[1]}>"
    recipient = to or (DemoAccount.display_name, DemoAccount.email)
    msg["To"] = f"{recipient[0]} <{recipient[1]}>"
    msg["Subject"] = subject
    msg["Date"] = formatdate(time.time() - age, localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender[1].split("@")[1])
    if extra == "receipt":
        msg.set_content("Order confirmed. Total 1 047 kr. Order number 2026-11-0042.")
        msg.add_alternative(_RECEIPT, subtype="html")
    elif extra == "html":
        msg.set_content(_NEWSLETTER, subtype="html")
        msg.add_related(_SKETCH, maintype="image", subtype="svg+xml", cid="<banner@demo>")
    elif body is not None:
        msg.set_content(body)
    if extra == "attachment":
        msg.add_attachment(_SKETCH, maintype="image", subtype="svg+xml", filename="screenshot-sketch.svg")
        msg.add_attachment(b"Release 0.4\n\n- Keyboard shortcuts\n- Faster search\n",
                           maintype="text", subtype="plain", filename="release-notes.txt")
    return msg.as_bytes()


_NETWORK_METHODS = {
    "new_arrivals", "list_folders", "unread_counts", "list_messages", "sync", "search", "fetch_raw",
    "fetch_raw_many", "set_flags", "move", "delete", "append", "send",
}


class DemoBackend:
    def __init__(self, account):
        self.account = account
        entries = [
            ("INBOX", "/", set()), ("[Mail]", "/", {"\\noselect"}),
            ("[Mail]/Drafts", "/", {"\\drafts"}), ("[Mail]/Sent", "/", {"\\sent"}),
            ("Archive", "/", {"\\archive"}), ("[Mail]/Junk", "/", {"\\junk"}),
            ("[Mail]/Trash", "/", {"\\trash"}), ("Projects", "/", set()),
            ("Projects/Tideline", "/", set()), ("Receipts", "/", set()),
        ]
        self._folders = build_folders(entries)
        self._mail: dict[str, dict[int, tuple[set, bytes]]] = {f.name: {} for f in self._folders}
        self._next_uid = 1
        # Flip to False (or start with BREV_DEMO_OFFLINE=1) to simulate a lost connection.
        self.online = os.environ.get("BREV_DEMO_OFFLINE") != "1"
        self.uidvalidity = {}
        for folder, age, sender, to, subject, body, flags, extra in sorted(_SAMPLES, key=lambda s: -s[1]):
            self._store(folder, _build_message(age, sender, to, subject, body, extra), set(flags))

    def _net(self):
        if not self.online:
            raise ConnectionError("demo network is down")

    def __getattribute__(self, name):
        if name in _NETWORK_METHODS:
            object.__getattribute__(self, "_net")()
        return object.__getattribute__(self, name)

    def _store(self, folder, raw, flags):
        uid = self._next_uid
        self._next_uid += 1
        self._mail[folder][uid] = (flags, raw)
        return uid

    def _summaries(self, folder, uids):
        parser = BytesHeaderParser()
        box = self._mail[folder]
        result = [summary_from_headers(uid, set(box[uid][0]), parser.parsebytes(box[uid][1]),
                                       size=len(box[uid][1])) for uid in uids]
        result.sort(key=lambda s: s.uid, reverse=True)
        return result

    def list_folders(self, with_counts=True):
        time.sleep(0.2)
        folders = [f for f in self._folders]
        if with_counts:
            counts = self.unread_counts([f.name for f in folders])
            for f in folders:
                f.unread = counts.get(f.name, 0) if f.counts_unread else 0
        return folders

    def unread_counts(self, names):
        return {n: sum(1 for flags, _raw in self._mail[n].values() if "\\Seen" not in flags) for n in names}

    def list_messages(self, folder, limit=50, older_than=None, unread_only=False):
        time.sleep(0.25)
        box = self._mail[folder]
        uids = sorted(u for u in box if (older_than is None or u < older_than)
                      and not (unread_only and "\\Seen" in box[u][0]))
        page = uids[-limit:]
        return self._summaries(folder, page), len(uids) > len(page)

    def sync(self, folder, known_uids):
        known = set(known_uids)
        top = max(known, default=0)
        box = self._mail[folder]
        new = [u for u in box if u > top]
        return self._summaries(folder, new), {u: set(box[u][0]) for u in known if u in box}

    def search(self, folder, query, limit=100):
        time.sleep(0.2)
        words = query.casefold().split()
        box = self._mail[folder]
        hits = [u for u, (_f, raw) in box.items()
                if all(w in raw.decode("utf-8", "replace").casefold() for w in words)]
        return self._summaries(folder, sorted(hits)[-limit:])

    def fetch_raw(self, folder, uid):
        time.sleep(0.15)
        return self._mail[folder][uid][1]

    def new_arrivals(self, folder, since):
        box = self._mail[folder]
        top = max(box, default=0)
        if since is None:
            return 0, top
        return sum(1 for u, (flags, _raw) in box.items() if u > since and "\\Seen" not in flags), max(top, since)

    def fetch_raw_many(self, folder, uids):
        return {uid: self._mail[folder][uid][1] for uid in uids if uid in self._mail[folder]}

    def set_flags(self, folder, uids, flag, value):
        for uid in uids:
            flags = self._mail[folder][uid][0]
            (flags.add if value else flags.discard)(flag)

    def move(self, folder, uids, destination):
        for uid in uids:
            flags, raw = self._mail[folder].pop(uid)
            self._store(destination, raw, flags)

    def delete(self, folder, uids):
        for uid in uids:
            self._mail[folder].pop(uid, None)

    def append(self, folder, raw, flags="(\\Seen)"):
        self._store(folder, raw, set(flags.strip("()").split()))

    def send(self, message):
        time.sleep(0.8)

    def close(self):
        pass


def create_services(cache):
    from .offline import CachingBackend
    from .service import MailService

    account = DemoAccount()
    return [MailService(account, CachingBackend(account, DemoBackend(account), cache))]
