"""Contacts from GNOME Online Accounts (CardDAV): read, cached locally, and editable.

GOA supplies the sign-in and the address book's address; the contacts themselves are
read and written with CardDAV (RFC 6352). Edits change only the fields Brev shows and
copy everything else in the vCard (photos, birthdays, notes, labels…) unchanged. Every
write carries the card's ETag, so a contact changed elsewhere is never overwritten.
"""

from __future__ import annotations

import base64
import copy
import logging
import quopri
import re
import ssl
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field, fields
from gettext import gettext as _

from gi.repository import GLib, GObject

from .accounts import AccountError, goa_credentials
from .service import run_in_thread

log = logging.getLogger(__name__)

_NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:carddav"}
_MULTIGET_BATCH = 200


class ContactConflict(Exception):
    """The contact changed on the server since it was loaded (HTTP 412)."""


@dataclass
class Contact:
    uid: str
    name: str = ""
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    org: str = ""
    source: str = ""
    # Where the card lives and what it looked like when loaded, for safe edits.
    href: str = ""
    etag: str = ""
    raw: str = ""
    book: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "Contact":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def label(self) -> str:
        return self.name or (self.emails[0] if self.emails else "") or self.org or \
            (self.phones[0] if self.phones else _("Unnamed Contact"))

    @property
    def sort_key(self) -> str:
        return GLib.utf8_collate_key(self.label.casefold(), -1)

    @property
    def initial(self) -> str:
        """Section letter, following the locale's collation.

        Å is filed under A where it sorts with A (English), but keeps its own
        section where it is a separate letter after Z (Swedish).
        """
        first = self.label[:1].upper()
        if not first.isalpha():
            return "#"
        base = unicodedata.normalize("NFKD", first)[0]
        if base != first and base.isascii() and base.isalpha():
            following = chr(ord(base) + 1) if base != "Z" else "ZZ"
            if GLib.utf8_collate(first, following) < 0:
                return base
        return first


# --- vCard reading -------------------------------------------------------------

def _unescape(value: str) -> str:
    return re.sub(r"\\([\\,;nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def _escape(value: str) -> str:
    return (value.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _unique(values, key):
    seen = set()
    result = []
    for value in values:
        k = key(value)
        if k not in seen:
            seen.add(k)
            result.append(value)
    return result


def _phone_key(phone: str) -> str:
    return re.sub(r"[^\d+]", "", phone)


def _unfold(text: str) -> str:
    return re.sub(r"\r?\n[ \t]", "", text)


def _split_line(line: str):
    """'item1.EMAIL;TYPE=WORK:a@b' -> ('item1', 'EMAIL', 'TYPE=WORK', 'a@b'). None if not a property."""
    if ":" not in line:
        return None
    head, value = line.split(":", 1)
    parts = head.split(";")
    group, _dot, prop = parts[0].rpartition(".")
    return group.lower(), prop.upper(), ";".join(parts[1:]), value


def _decode_value(params: str, value: str) -> str:
    if "QUOTED-PRINTABLE" in params.upper():
        charset = re.search(r"CHARSET=([\w-]+)", params, re.I)
        value = quopri.decodestring(value.encode()).decode(charset.group(1) if charset else "utf-8", "replace")
    return value


def parse_vcard(card: str, source: str = "", fallback_uid: str = "") -> Contact | None:
    contact = Contact(uid="", source=source, raw=card)
    structured = ""
    for line in _unfold(card).splitlines():
        parsed = _split_line(line)
        if parsed is None:
            continue
        _group, prop, params, value = parsed
        value = _decode_value(params, value)
        if prop == "FN":
            contact.name = _unescape(value).strip()
        elif prop == "N":
            parts = [_unescape(f).strip() for f in re.split(r"(?<!\\);", value)]
            family = parts[0] if parts else ""
            given = parts[1] if len(parts) > 1 else ""
            structured = " ".join(filter(None, [given, family]))
        elif prop == "EMAIL" and value.strip():
            contact.emails.append(_unescape(value).strip())
        elif prop == "TEL" and value.strip():
            contact.phones.append(_unescape(value).strip().removeprefix("tel:"))
        elif prop == "ORG":
            contact.org = _unescape(re.split(r"(?<!\\);", value)[0]).strip()
        elif prop == "UID":
            contact.uid = value.strip()
    contact.name = contact.name or structured
    # Providers often repeat an address once per TYPE (home, work, other…).
    contact.emails = _unique(contact.emails, key=str.casefold)
    contact.phones = _unique(contact.phones, key=_phone_key)
    contact.uid = contact.uid or fallback_uid or f"{source}-{contact.label}"
    if contact.name or contact.emails or contact.phones or contact.org:
        return contact
    return None


def parse_vcards(text: str, source: str = "") -> list[Contact]:
    contacts = []
    for index, block in enumerate(re.findall(r"BEGIN:VCARD.*?END:VCARD", text, re.S | re.I)):
        contact = parse_vcard(block, source, f"{source}-{index}")
        if contact is not None:
            contacts.append(contact)
    return contacts


# --- vCard writing -----------------------------------------------------------------

def _fold(line: str) -> str:
    """Fold to 75 octets per line (RFC 6350 §3.2) without splitting UTF-8 characters."""
    data = line.encode()
    if len(data) <= 75:
        return line
    pieces, limit = [], 75
    while len(data) > limit:
        cut = limit
        while cut > 0 and (data[cut] & 0xC0) == 0x80:
            cut -= 1
        pieces.append(data[:cut].decode())
        data = data[cut:]
        limit = 74  # continuation lines start with a space
    pieces.append(data.decode())
    return "\r\n ".join(pieces)


def _split_name(full: str) -> tuple[str, str]:
    words = full.split()
    if len(words) < 2:
        return full.strip(), ""
    return " ".join(words[:-1]), words[-1]


def _n_line(name: str) -> str:
    given, family = _split_name(name)
    return f"N:{_escape(family)};{_escape(given)};;;"


def new_vcard(contact: Contact) -> str:
    lines = ["BEGIN:VCARD", "VERSION:3.0", f"UID:{contact.uid}",
             f"FN:{_escape(contact.name or contact.label)}", _n_line(contact.name)]
    if contact.org:
        lines.append(f"ORG:{_escape(contact.org)}")
    lines += [f"EMAIL;TYPE=INTERNET:{_escape(e)}" for e in contact.emails]
    lines += [f"TEL:{_escape(p)}" for p in contact.phones]
    lines.append("END:VCARD")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def update_vcard(raw: str, contact: Contact) -> str:
    """Apply the edited name, organisation, emails and phones to an existing card.

    Unchanged addresses and numbers keep their original lines (types, labels, order);
    everything Brev does not edit is copied through as it was.
    """
    lines = _unfold(raw).splitlines()
    version_4 = any(re.fullmatch(r"VERSION:4\.0", l.strip(), re.I) for l in lines)
    old = parse_vcard(raw) or Contact(uid="")
    name_changed = contact.name.strip() != old.name.strip()
    wanted_emails = {e.casefold(): e for e in contact.emails}
    wanted_phones = {_phone_key(p): p for p in contact.phones}
    kept_emails, kept_phones = set(), set()
    removed_groups = set()
    out = []
    saw_fn = saw_org = False

    for line in lines:
        parsed = _split_line(line)
        if parsed is None:
            out.append(line)
            continue
        group, prop, params, value = parsed
        plain = _unescape(_decode_value(params, value)).strip()
        if prop == "FN":
            saw_fn = True
            out.append(f"FN:{_escape(contact.name or contact.label)}" if name_changed else line)
        elif prop == "N":
            out.append(_n_line(contact.name) if name_changed else line)
        elif prop == "ORG":
            saw_org = True
            if contact.org:
                rest = re.split(r"(?<!\\);", value)[1:]  # keep department etc.
                out.append(";".join([f"ORG:{_escape(contact.org)}"] + rest)
                           if contact.org != old.org else line)
            elif group:
                removed_groups.add(group)
        elif prop == "EMAIL":
            key = plain.casefold()
            if key in wanted_emails and key not in kept_emails:
                kept_emails.add(key)
                out.append(line)
            elif group:
                removed_groups.add(group)
        elif prop == "TEL":
            key = _phone_key(plain.removeprefix("tel:"))
            if key in wanted_phones and key not in kept_phones:
                kept_phones.add(key)
                out.append(line)
            elif group:
                removed_groups.add(group)
        elif prop == "END":
            if not saw_fn:
                out.append(f"FN:{_escape(contact.name or contact.label)}")
            if contact.org and not saw_org:
                out.append(f"ORG:{_escape(contact.org)}")
            for key, email in wanted_emails.items():
                if key not in kept_emails:
                    out.append(f"EMAIL:{_escape(email)}" if version_4 else f"EMAIL;TYPE=INTERNET:{_escape(email)}")
            for key, phone in wanted_phones.items():
                if key not in kept_phones:
                    out.append(f"TEL:{_escape(phone)}")
            out.append(line)
        else:
            out.append(line)

    if removed_groups:  # labels and the like that belonged to a removed address
        out = [l for l in out if not ((p := _split_line(l)) and p[0] and p[0] in removed_groups)]
    return "\r\n".join(_fold(line) for line in out) + "\r\n"


# --- CardDAV -----------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class CardDav:
    """Just enough CardDAV to list, read and change contacts (RFC 6352)."""

    def __init__(self, start_uri: str, authorization: str, verify_tls=True):
        self.start_uri = start_uri
        self._auth = authorization
        context = ssl.create_default_context()
        if not verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        self._opener = urllib.request.build_opener(
            _NoRedirect, urllib.request.HTTPSHandler(context=context), urllib.request.HTTPHandler())

    def _open(self, method, url, body: bytes | None, headers: dict):
        """Returns (final url, response). Follows redirects itself, keeping method and auth."""
        for _hop in range(6):
            request = urllib.request.Request(url, data=body, method=method,
                                             headers={"Authorization": self._auth, **headers})
            try:
                return url, self._opener.open(request, timeout=30)
            except urllib.error.HTTPError as err:
                if err.code in (301, 302, 307, 308) and err.headers.get("Location"):
                    url = urllib.parse.urljoin(url, err.headers["Location"])
                    continue
                if err.code == 412:
                    raise ContactConflict() from err
                raise
        raise OSError("Too many redirects")

    def _xml(self, method, url, body: str, depth: str):
        url, response = self._open(method, url, body.encode(), {
            "Depth": depth, "Content-Type": "application/xml; charset=utf-8"})
        with response:
            return response.geturl() or url, ET.fromstring(response.read())

    def _propfind(self, url, props, depth):
        body = ('<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav"><d:prop>'
                + "".join(f"<{p}/>" for p in props) + "</d:prop></d:propfind>")
        return self._xml("PROPFIND", url, body, depth)

    @staticmethod
    def _responses(root):
        for response in root.findall("d:response", _NS):
            href = response.findtext("d:href", default="", namespaces=_NS)
            for propstat in response.findall("d:propstat", _NS):
                if " 200 " in (propstat.findtext("d:status", default="", namespaces=_NS) + " "):
                    yield href, propstat.find("d:prop", _NS)

    def address_books(self) -> list[str]:
        url, root = self._propfind(self.start_uri, ["d:resourcetype", "d:current-user-principal"], "0")
        principal = None
        for href, prop in self._responses(root):
            if prop.find("d:resourcetype/c:addressbook", _NS) is not None:
                return [urllib.parse.urljoin(url, href)]
            principal = principal or prop.findtext("d:current-user-principal/d:href", namespaces=_NS)
        principal = urllib.parse.urljoin(url, principal) if principal else url
        url, root = self._propfind(principal, ["c:addressbook-home-set"], "0")
        home = None
        for _href, prop in self._responses(root):
            home = home or prop.findtext("c:addressbook-home-set/d:href", namespaces=_NS)
        home = urllib.parse.urljoin(url, home) if home else url
        url, root = self._propfind(home, ["d:resourcetype"], "1")
        return [urllib.parse.urljoin(url, href) for href, prop in self._responses(root)
                if prop.find("d:resourcetype/c:addressbook", _NS) is not None]

    def cards(self, book_url: str) -> list[tuple[str, str, str]]:
        """(absolute href, etag, vCard text) for every card in an address book."""
        url, root = self._propfind(book_url, ["d:getetag", "d:resourcetype"], "1")
        hrefs = [href for href, prop in self._responses(root)
                 if prop.find("d:resourcetype/d:collection", _NS) is None
                 and urllib.parse.urljoin(url, href).rstrip("/") != url.rstrip("/")]
        result = []
        for start in range(0, len(hrefs), _MULTIGET_BATCH):
            body = ('<c:addressbook-multiget xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">'
                    "<d:prop><d:getetag/><c:address-data/></d:prop>"
                    + "".join(f"<d:href>{GLib.markup_escape_text(h)}</d:href>"
                              for h in hrefs[start:start + _MULTIGET_BATCH])
                    + "</c:addressbook-multiget>")
            _url, report = self._xml("REPORT", url, body, "1")
            for href, prop in self._responses(report):
                result.append((urllib.parse.urljoin(url, href),
                               prop.findtext("d:getetag", default="", namespaces=_NS),
                               prop.findtext("c:address-data", default="", namespaces=_NS)))
        return result

    def get(self, url: str) -> tuple[str, str, str]:
        url, response = self._open("GET", url, None, {"Accept": "text/vcard"})
        with response:
            return url, response.headers.get("ETag", ""), response.read().decode("utf-8", "replace")

    def put(self, url: str, card: str, etag: str | None) -> str:
        """Create (etag None: must not exist yet) or replace (only if still at etag). Returns the url used."""
        headers = {"Content-Type": "text/vcard; charset=utf-8"}
        if etag:
            headers["If-Match"] = etag
        else:
            headers["If-None-Match"] = "*"
        final, response = self._open("PUT", url, card.encode(), headers)
        with response:
            location = response.headers.get("Location")
        return urllib.parse.urljoin(final, location) if location else final

    def delete(self, url: str, etag: str):
        _url, response = self._open("DELETE", url, None, {"If-Match": etag} if etag else {})
        response.close()


# --- sources ---------------------------------------------------------------------------

class GoaContactSource:
    writable = True

    def __init__(self, obj):
        account = obj.get_account()
        contacts = obj.get_contacts()
        self._object = obj
        self.id = account.props.id
        self.title = account.props.presentation_identity
        self.uri = contacts.props.uri
        self.verify_tls = not contacts.props.accept_ssl_errors
        self._books: list[str] = []

    def _dav(self) -> CardDav:
        kind, secret = goa_credentials(self._object, self.title, "password")
        if kind == "oauth2":
            auth = "Bearer " + secret
        else:
            user = self._object.get_account().props.identity
            auth = "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()
        return CardDav(self.uri, auth, self.verify_tls)

    def fetch(self) -> list[Contact]:
        dav = self._dav()
        self._books = dav.address_books()
        contacts = []
        for book in self._books:
            for href, etag, card in dav.cards(book):
                contact = parse_vcard(card, self.id, href)
                if contact is not None:
                    contact.href, contact.etag, contact.book = href, etag, book
                    contacts.append(contact)
        return contacts

    def _reload(self, dav, contact, url) -> Contact:
        """Read a card back after writing it: the server may have added to it."""
        final, etag, card = dav.get(url)
        fresh = parse_vcard(card, self.id, contact.uid) or contact
        fresh.href, fresh.etag, fresh.book = final, etag, contact.book
        return fresh

    def create(self, contact: Contact) -> Contact:
        dav = self._dav()
        if not self._books:
            self._books = dav.address_books()
        if not self._books:
            raise OSError(_("This account has no address book."))
        contact = copy.deepcopy(contact)
        contact.uid = contact.uid or str(uuid.uuid4())
        contact.book = contact.book or self._books[0]
        url = urllib.parse.urljoin(contact.book.rstrip("/") + "/", contact.uid + ".vcf")
        contact.raw = new_vcard(contact)
        return self._reload(dav, contact, dav.put(url, contact.raw, None))

    @staticmethod
    def _ensure_unchanged(dav, contact: Contact):
        """Refuse to write over a contact that changed (or vanished) since it was loaded.

        Servers should do this themselves via If-Match, but Google's CardDAV ignores it,
        so check the current ETag first. (If-Match is still sent for servers that honour it.)
        """
        try:
            _url, etag, _card = dav.get(contact.href)
        except urllib.error.HTTPError as err:
            if err.code in (404, 410):
                raise ContactConflict() from err
            raise
        if contact.etag and etag and etag != contact.etag:
            raise ContactConflict()

    def update(self, contact: Contact) -> Contact:
        dav = self._dav()
        self._ensure_unchanged(dav, contact)
        card = update_vcard(contact.raw, contact)
        return self._reload(dav, contact, dav.put(contact.href, card, contact.etag or None))

    def delete(self, contact: Contact):
        dav = self._dav()
        self._ensure_unchanged(dav, contact)
        dav.delete(contact.href, contact.etag)


def load_sources(client) -> list[GoaContactSource]:
    sources = []
    for obj in client.get_accounts():
        if obj.get_contacts() is None or obj.get_account().props.contacts_disabled:
            continue
        if obj.get_contacts().props.uri:
            sources.append(GoaContactSource(obj))
    return sources


class DemoContactSource:
    """In-memory contacts for the demo account; edits last until Brev quits."""

    id = "demo"
    title = "Demo"
    writable = True
    _CARDS = [
        ("Maja Lindqvist", ["maja@example.net"], ["+46 70 123 45 67"]),
        ("Jonas Berg", ["jonas@example.com", "jonas.berg@work.example"], []),
        ("Priya Natarajan", ["priya@example.com"], []),
        ("Erik Holm", ["erik@example.net"], ["+46 73 555 01 02"]),
        ("Ana Sousa", ["ana@example.pt"], []),
        ("Åsa Ekström", ["asa@example.se"], []),
        ("Örjan Nilsson", [], ["+46 70 987 65 43"]),
        ("Björn Andersson", ["bjorn@example.se"], []),
        ("Chen Wei", ["chen.wei@example.cn"], []),
        ("Dentist Clinic", ["calendar@example.org"], ["+46 8 123 456"]),
    ]
    _store: dict | None = None

    def __init__(self):
        if DemoContactSource._store is None:
            DemoContactSource._store = {}
            for i, (n, e, p) in enumerate(self._CARDS):
                c = Contact(uid=f"demo-{i}", name=n, emails=list(e), phones=list(p), source="demo")
                self._save(c)

    def _save(self, contact):
        contact.raw = new_vcard(contact)
        contact.href = f"demo:{contact.uid}"
        contact.etag = uuid.uuid4().hex
        DemoContactSource._store[contact.uid] = copy.deepcopy(contact)
        return copy.deepcopy(contact)

    def fetch(self):
        return [copy.deepcopy(c) for c in DemoContactSource._store.values()]

    def create(self, contact):
        contact = copy.deepcopy(contact)
        contact.uid, contact.source = str(uuid.uuid4()), "demo"
        return self._save(contact)

    def update(self, contact):
        current = DemoContactSource._store.get(contact.uid)
        if current is None or current.etag != contact.etag:
            raise ContactConflict()
        return self._save(copy.deepcopy(contact))

    def delete(self, contact):
        current = DemoContactSource._store.get(contact.uid)
        if current is None or current.etag != contact.etag:
            raise ContactConflict()
        del DemoContactSource._store[contact.uid]


# --- the book ------------------------------------------------------------------------

def _clean(contact: Contact) -> Contact:
    """Also tidy contacts cached before duplicates were merged at parse time."""
    contact.emails = _unique(contact.emails, key=str.casefold)
    contact.phones = _unique(contact.phones, key=_phone_key)
    return contact


class ContactBook(GObject.Object):
    """All known contacts. Emits "changed" after a refresh or an edit."""

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, cache):
        super().__init__()
        self._cache = cache
        self.contacts: list[Contact] = []
        self.sources: dict[str, object] = {}
        self.refreshing = False
        self.busy = False
        self._set([_clean(Contact.from_dict(data)) for data in cache.load_contacts()])

    def _set(self, contacts):
        self.contacts = sorted(contacts, key=lambda c: c.sort_key)
        self.emit("changed")

    def _reload_from_cache(self):
        return [_clean(Contact.from_dict(data)) for data in self._cache.load_contacts()]

    def refresh(self, sources, done=None):
        """Re-read every source in the background; failed sources keep their cached cards."""
        self.sources = {s.id: s for s in sources}
        if self.refreshing:
            return
        self.refreshing = True
        self.emit("changed")
        cache = self._cache

        def work():
            errors = []
            for source in sources:
                try:
                    cards = source.fetch()
                except (AccountError, OSError, ET.ParseError) as err:
                    log.warning("Could not load contacts from %s: %s", source.title, err)
                    errors.append((source, err))
                    continue
                cache.replace_contacts(source.id, [asdict(c) for c in cards])
            cache.forget_contact_sources([s.id for s in sources])
            return self._reload_from_cache(), errors

        def finished(result):
            self.refreshing = False
            contacts, errors = result
            self._set(contacts)
            if done:
                done(errors)

        def failed(err):
            self.refreshing = False
            self.emit("changed")
            if done:
                done([(None, err)])

        run_in_thread(work, finished, failed)

    # --- editing ---------------------------------------------------------

    def writable_sources(self) -> list:
        return [s for s in self.sources.values() if getattr(s, "writable", False)]

    def _write(self, source_id, change, done, error):
        """Run change(source) -> list of contacts for that source, then store and announce it."""
        source = self.sources.get(source_id)
        if source is None:
            error(OSError(_("This contact’s account is not available.")))
            return
        cache = self._cache
        self.busy = True

        def work():
            mine = [c for c in self._reload_from_cache() if c.source == source_id]
            updated = change(source, mine)
            cache.replace_contacts(source_id, [asdict(c) for c in updated])
            return self._reload_from_cache()

        def finished(contacts):
            self.busy = False
            self._set(contacts)
            done()

        def failed(err):
            self.busy = False
            if isinstance(err, ContactConflict):
                self.refresh(list(self.sources.values()))  # get the current version
            error(err)

        run_in_thread(work, finished, failed)

    def add(self, source_id, contact, done, error):
        def change(source, mine):
            return mine + [source.create(contact)]
        self._write(source_id, change, done, error)

    def save(self, contact, done, error):
        def change(source, mine):
            fresh = source.update(contact)
            return [fresh if c.uid == contact.uid else c for c in mine]
        self._write(contact.source, change, done, error)

    def remove(self, contact, done, error):
        def change(source, mine):
            source.delete(contact)
            return [c for c in mine if c.uid != contact.uid]
        self._write(contact.source, change, done, error)

    def lookup(self, email: str) -> Contact | None:
        """The contact that has this address, if any."""
        key = email.strip().casefold()
        if not key:
            return None
        return next((c for c in self.contacts if any(e.casefold() == key for e in c.emails)), None)

    # --- suggestions -------------------------------------------------------

    def suggest(self, query: str, exclude=(), limit: int = 8) -> list[tuple[str, str]]:
        """(name, email) pairs matching what the user typed, best first."""
        query = query.strip().casefold()
        if not query:
            return []
        excluded = {e.casefold() for e in exclude}
        scored = []
        for contact in self.contacts:
            name = contact.name.casefold()
            words = name.split()
            for email in contact.emails:
                address = email.casefold()
                if address in excluded:
                    continue
                if name.startswith(query):
                    score = 0
                elif any(w.startswith(query) for w in words):
                    score = 1
                elif address.startswith(query):
                    score = 2
                elif query in address or query in name:
                    score = 3
                else:
                    continue
                scored.append((score, contact.sort_key, contact.name, email))
        scored.sort()
        # One suggestion per address, even if several cards (or one card twice) list it.
        result, seen = [], set()
        for _score, _key, name, email in scored:
            if email.casefold() in seen:
                continue
            seen.add(email.casefold())
            result.append((name, email))
            if len(result) == limit:
                break
        return result
