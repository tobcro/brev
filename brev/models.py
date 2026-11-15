"""Plain data types shared by the backends and the UI."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
from gettext import gettext as _


@dataclass(frozen=True)
class Address:
    name: str
    email: str

    @property
    def label(self) -> str:
        return self.name or self.email

    def format(self) -> str:
        """Human-editable form, e.g. for the compose fields."""
        if not self.name:
            return self.email
        name = self.name
        if re.search(r'[,;:<>@"()\[\]\\]', name):
            name = '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return f"{name} <{self.email}>"


ROLE_ORDER = ("inbox", "flagged", "drafts", "sent", "archive", "all", "junk", "trash")

ROLE_ICONS = {
    "inbox": "mail-inbox-symbolic",
    "flagged": "starred-symbolic",
    "drafts": "document-edit-symbolic",
    "sent": "mail-send-symbolic",
    "archive": "mail-archive-symbolic",
    "all": "mail-archive-symbolic",
    "junk": "mail-mark-junk-symbolic",
    "trash": "user-trash-symbolic",
}

_SPECIAL_USE = {
    "\\sent": "sent",
    "\\drafts": "drafts",
    "\\trash": "trash",
    "\\junk": "junk",
    "\\archive": "archive",
    "\\all": "all",
    "\\flagged": "flagged",
}

# Fallbacks for servers that do not advertise RFC 6154 special-use flags.
_NAME_ROLES = {
    "sent": "sent",
    "sent items": "sent",
    "sent messages": "sent",
    "sent mail": "sent",
    "drafts": "drafts",
    "draft": "drafts",
    "trash": "trash",
    "bin": "trash",
    "deleted items": "trash",
    "deleted messages": "trash",
    "junk": "junk",
    "junk e-mail": "junk",
    "junk email": "junk",
    "spam": "junk",
    "archive": "archive",
    "archives": "archive",
}


def role_title(role: str) -> str:
    return {
        "inbox": _("Inbox"),
        "flagged": _("Starred"),
        "drafts": _("Drafts"),
        "sent": _("Sent"),
        "archive": _("Archive"),
        "all": _("All Mail"),
        "junk": _("Junk"),
        "trash": _("Trash"),
    }[role]


@dataclass
class Folder:
    name: str  # raw IMAP mailbox name, used in commands
    path: str  # decoded full path
    display_name: str
    delimiter: str | None
    role: str | None = None
    depth: int = 0
    unread: int = 0

    @property
    def title(self) -> str:
        return role_title(self.role) if self.role else self.display_name

    @property
    def icon_name(self) -> str:
        return ROLE_ICONS.get(self.role, "folder-symbolic")

    @property
    def counts_unread(self) -> bool:
        return self.role not in ("drafts", "sent", "all", "junk", "trash")


@dataclass
class MessageSummary:
    uid: int
    flags: set[str]
    timestamp: float
    subject: str
    sender: Address | None
    to: list[Address] = field(default_factory=list)
    cc: list[Address] = field(default_factory=list)
    reply_to: list[Address] = field(default_factory=list)
    message_id: str = ""
    in_reply_to: str = ""
    references: str = ""
    size: int = 0
    has_attachments: bool = False

    @property
    def unread(self) -> bool:
        return "\\Seen" not in self.flags

    @property
    def flagged(self) -> bool:
        return "\\Flagged" in self.flags


# --- header helpers -------------------------------------------------------

def decode_text(value) -> str:
    """Decode an RFC 2047 header value into clean text."""
    if value is None:
        return ""
    if isinstance(value, str):
        value = re.sub(r"\r?\n(?=[ \t])", "", value)
    try:
        text = str(make_header(decode_header(value)))
    except Exception:
        text = str(value)
    # Raw 8-bit headers arrive as surrogate-escaped str; assume UTF-8.
    text = text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    return re.sub(r"\s+", " ", text).strip()


def parse_addresses(values) -> list[Address]:
    raws = [str(v) for v in (values or []) if v]
    result = []
    for name, addr in getaddresses(raws):
        if not addr and not name:
            continue
        result.append(Address(decode_text(name), decode_text(addr)))
    return result


def summary_from_headers(uid, flags, headers, fallback_ts=0.0, size=0) -> MessageSummary:
    """Build a summary from an email.message.Message holding just headers."""
    timestamp = fallback_ts
    date = headers.get("Date")
    if date:
        try:
            timestamp = parsedate_to_datetime(decode_text(date)).timestamp()
        except Exception:
            pass
    senders = parse_addresses(headers.get_all("From"))
    ctype = (headers.get("Content-Type") or "").lower()
    return MessageSummary(
        uid=uid,
        flags=set(flags),
        timestamp=timestamp,
        subject=decode_text(headers.get("Subject")),
        sender=senders[0] if senders else None,
        to=parse_addresses(headers.get_all("To")),
        cc=parse_addresses(headers.get_all("Cc")),
        reply_to=parse_addresses(headers.get_all("Reply-To")),
        message_id=decode_text(headers.get("Message-ID")),
        in_reply_to=decode_text(headers.get("In-Reply-To")),
        references=decode_text(headers.get("References")),
        size=size,
        has_attachments=ctype.startswith("multipart/mixed"),
    )


# --- folders --------------------------------------------------------------

def decode_mutf7(name: str) -> str:
    """Decode an IMAP modified UTF-7 mailbox name (RFC 3501 §5.1.3)."""
    out = []
    i = 0
    while i < len(name):
        char = name[i]
        if char != "&":
            out.append(char)
            i += 1
            continue
        end = name.find("-", i)
        if end == -1:
            out.append(name[i:])
            break
        if end == i + 1:
            out.append("&")
        else:
            chunk = name[i + 1:end].replace(",", "/")
            chunk += "=" * (-len(chunk) % 4)
            try:
                out.append(base64.b64decode(chunk).decode("utf-16-be"))
            except Exception:
                out.append(name[i:end + 1])
        i = end + 1
    return "".join(out)


def build_folders(entries) -> list[Folder]:
    """Turn (raw name, delimiter, lowercase flags) tuples into sorted folders."""
    folders = []
    for name, delimiter, flags in entries:
        if "\\noselect" in flags or "\\nonexistent" in flags:
            continue
        path = decode_mutf7(name)
        if name.upper() == "INBOX":
            role = "inbox"
        else:
            role = next((_SPECIAL_USE[f] for f in flags if f in _SPECIAL_USE), None)
        display = path.rsplit(delimiter, 1)[-1] if delimiter else path
        folders.append(Folder(name, path, display, delimiter, role))

    taken = {f.role for f in folders if f.role}
    for folder in folders:
        if folder.role:
            continue
        parent = folder.path.rsplit(folder.delimiter, 1)[0] if folder.delimiter and folder.delimiter in folder.path else ""
        if parent and parent.upper() != "INBOX":
            continue
        role = _NAME_ROLES.get(folder.display_name.casefold())
        if role and role not in taken:
            folder.role = role
            taken.add(role)

    plain_paths = {f.path for f in folders if not f.role}
    for folder in folders:
        if folder.role or not folder.delimiter:
            continue
        parts = folder.path.split(folder.delimiter)
        folder.depth = sum(
            1 for i in range(1, len(parts)) if folder.delimiter.join(parts[:i]) in plain_paths
        )

    def key(folder):
        rank = ROLE_ORDER.index(folder.role) if folder.role else len(ROLE_ORDER)
        return rank, folder.path.casefold()

    folders.sort(key=key)
    return folders
