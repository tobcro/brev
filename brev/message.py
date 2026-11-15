"""Turning raw MIME messages into something the message view can show."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from gettext import gettext as _
from html.parser import HTMLParser
from typing import NamedTuple

MAX_BODY_CHARS = 250_000


class Style(NamedTuple):
    bold: bool = False
    italic: bool = False
    href: str | None = None
    dim: bool = False


PLAIN = Style()


@dataclass
class Attachment:
    filename: str
    content_type: str
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass
class MessageContent:
    raw: bytes
    text: str  # plain text, used for quoting in replies
    markup: str  # Pango markup for display
    html: str | None
    has_plain: bool
    attachments: list[Attachment] = field(default_factory=list)
    inline: dict[str, tuple[str, bytes]] = field(default_factory=dict)  # Content-ID -> (type, data)
    remote_content: bool = False  # the HTML references images or styles on the internet


# --- text runs -------------------------------------------------------------

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_URL_RE = re.compile(r"(?:https?://|mailto:|www\.)[^\s<>\"']+", re.I)


def _add(runs, text, style):
    if not text:
        return
    if runs and runs[-1][1] == style:
        runs[-1] = (runs[-1][0] + text, style)
    else:
        runs.append((text, style))


def plain_to_runs(text: str):
    runs = []
    for line in text.splitlines(keepends=True):
        dim = line.lstrip().startswith(">")
        base = Style(dim=dim)
        pos = 0
        for match in _URL_RE.finditer(line):
            url = match.group(0).rstrip(".,;:!?)]}'\"")
            start = match.start()
            _add(runs, line[pos:start], base)
            href = "https://" + url if url.lower().startswith("www.") else url
            _add(runs, url, Style(href=href, dim=dim))
            pos = start + len(url)
        _add(runs, line[pos:], base)
    return runs


class _HtmlToRuns(HTMLParser):
    SKIP = {"script", "style", "head", "title", "template", "noscript", "svg"}
    BLOCK_GAP = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "ul", "ol", "table", "pre"}
    BLOCK = {"div", "tr", "section", "article", "header", "footer", "dl", "dt", "dd", "center", "hr"}
    HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.runs = []
        self.skip = 0
        self.bold = 0
        self.italic = 0
        self.quote = 0
        self.pre = 0
        self.href = None

    def _style(self):
        return Style(self.bold > 0, self.italic > 0, self.href, self.quote > 0)

    def _break_style(self):
        return PLAIN

    def _trailing_newlines(self):
        if not self.runs:
            return None
        tail = "".join(text for text, _style in self.runs[-6:]).rstrip(" \t")
        return len(tail) - len(tail.rstrip("\n"))

    def _newlines(self, n):
        have = self._trailing_newlines()
        if have is None:  # nothing written yet
            return
        if self.runs:
            text, style = self.runs[-1]
            self.runs[-1] = (text.rstrip(" \t"), style)
        if n > have:
            _add(self.runs, "\n" * (n - have), self._break_style())

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
            return
        if tag == "br":
            if self.runs:
                text, style = self.runs[-1]
                self.runs[-1] = (text.rstrip(" \t"), style)
            _add(self.runs, "\n", self._break_style())
        elif tag in self.BLOCK_GAP:
            self._newlines(2)
        elif tag in self.BLOCK:
            self._newlines(1)
        if tag == "li":
            self._newlines(1)
            _add(self.runs, "• ", self._style())
        elif tag in ("td", "th"):
            if self.runs and not self.runs[-1][0].endswith(("\n", " ")):
                _add(self.runs, "  ", self._style())
        if tag in ("b", "strong") or tag in self.HEADINGS:
            self.bold += 1
        elif tag in ("i", "em"):
            self.italic += 1
        elif tag == "blockquote":
            self.quote += 1
        elif tag == "pre":
            self.pre += 1
        elif tag == "a":
            href = (dict(attrs).get("href") or "").strip()
            if re.match(r"(https?|mailto):", href, re.I):
                self.href = href

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if tag in ("b", "strong") or tag in self.HEADINGS:
            self.bold = max(0, self.bold - 1)
        elif tag in ("i", "em"):
            self.italic = max(0, self.italic - 1)
        elif tag == "blockquote":
            self.quote = max(0, self.quote - 1)
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
        elif tag == "a":
            self.href = None
        if tag in self.BLOCK_GAP:
            self._newlines(2)
        elif tag in self.BLOCK or tag == "li":
            self._newlines(1)

    def handle_data(self, data):
        if self.skip:
            return
        if not self.pre:
            data = re.sub(r"\s+", " ", data)
            at_line_start = not self.runs or self.runs[-1][0].endswith(("\n", " "))
            if at_line_start:
                data = data.lstrip(" ")
        _add(self.runs, data, self._style())


def html_to_runs(markup: str):
    parser = _HtmlToRuns()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        pass
    runs = parser.runs
    while runs and not runs[0][0].strip():
        runs.pop(0)
    while runs and not runs[-1][0].strip():
        runs.pop()
    return runs


def runs_to_markup(runs) -> str:
    out = []
    for text, style in runs:
        piece = html.escape(text, quote=False)
        if style.bold:
            piece = f"<b>{piece}</b>"
        if style.italic:
            piece = f"<i>{piece}</i>"
        if style.dim:
            piece = f'<span fgalpha="62%">{piece}</span>'
        if style.href:
            piece = f'<a href="{html.escape(style.href, quote=True)}">{piece}</a>'
        out.append(piece)
    return "".join(out)


def html_to_text(markup: str) -> str:
    return "".join(text for text, _style in html_to_runs(markup))


# --- parsing ---------------------------------------------------------------

_REMOTE_RE = re.compile(
    r"""(?:\b(?:src|background|poster|srcset)\s*=\s*["']?\s*(?:https?:)?//)"""
    r"""|(?:url\(\s*["']?\s*(?:https?:)?//)|(?:<link\b[^>]*\bhref\s*=\s*["']?\s*(?:https?:)?//)""",
    re.I)


def unflow(text: str, delsp: bool = False) -> str:
    """Reflow RFC 3676 format=flowed text into paragraphs."""
    out = []
    buffer, depth = None, 0

    def flush():
        nonlocal buffer
        if buffer is not None:
            out.append(("> " * depth if depth else "") + buffer)
            buffer = None

    for line in text.split("\n"):
        if line == "-- ":  # signature separator is never flowed
            flush()
            out.append(line)
            continue
        level = len(line) - len(line.lstrip(">"))
        content = line[level:]
        if content.startswith(" "):
            content = content[1:]  # space-stuffing
        soft = content.endswith(" ")
        if soft and delsp:
            content = content[:-1]
        if buffer is not None and level == depth:
            buffer += content
        else:
            flush()
            buffer, depth = content, level
        if not soft:
            flush()
    flush()
    return "\n".join(out)


def _part_text(part) -> str:
    try:
        text = part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, "replace")
        except LookupError:
            text = payload.decode("utf-8", "replace")
    if not isinstance(text, str):
        text = str(text)
    text = _CONTROL_RE.sub("", text.replace("\r\n", "\n"))
    if part.get_content_type() == "text/plain" and (part.get_param("format") or "").lower() == "flowed":
        text = unflow(text, (part.get_param("delsp") or "").lower() == "yes")
    return text


def _inline_parts(msg) -> dict[str, tuple[str, bytes]]:
    parts = {}
    for part in msg.walk():
        cid = part.get("Content-ID")
        if not cid or part.is_multipart():
            continue
        try:
            data = part.get_payload(decode=True) or b""
        except Exception:
            continue
        parts[str(cid).strip().strip("<>")] = (part.get_content_type(), data)
    return parts


# --- choosing what to show -----------------------------------------------------
#
# The body is worked out from the MIME tree rather than "the first HTML part
# anywhere": alternatives pick one rendering, related parts show their root,
# and mixed parts are shown in order. Attachments and forwarded messages are
# never treated as the body.

def _is_attachment(part) -> bool:
    return part.get_content_disposition() == "attachment" or part.get_content_type() == "message/rfc822"


def _leaves(part):
    """Leaf parts, without descending into attached messages."""
    if part.get_content_type() == "message/rfc822" or not part.is_multipart():
        yield part
        return
    for child in part.get_payload() or []:
        yield from _leaves(child)


def _offers(part, ctype: str) -> bool:
    return any(leaf.get_content_type() == ctype and not _is_attachment(leaf) for leaf in _leaves(part))


def _collect(part, prefer: str, used: set) -> list:
    """Segments to show, in order: ("text", str), ("html", str) or ("image", part)."""
    if _is_attachment(part):
        return []
    if part.is_multipart():
        children = [c for c in (part.get_payload() or []) if not _is_attachment(c)]
        if not children:
            return []
        subtype = part.get_content_subtype()
        if subtype == "alternative":
            wanted = "text/html" if prefer == "html" else "text/plain"
            ordered = list(reversed(children)) if prefer == "html" else children
            chosen = next((c for c in ordered if _offers(c, wanted)), children[-1])
            return _collect(chosen, prefer, used)
        if subtype == "related":
            start = (part.get_param("start") or "").strip("<>")
            root = next((c for c in children if (c.get("Content-ID") or "").strip().strip("<>") == start),
                        children[0]) if start else children[0]
            return _collect(root, prefer, used)
        segments = []
        for child in children:
            segments += _collect(child, prefer, used)
        return segments
    ctype = part.get_content_type()
    if ctype in ("text/plain", "text/html"):
        used.add(id(part))
        return [("html" if ctype == "text/html" else "text", _part_text(part))]
    if prefer == "html" and part.get_content_maintype() == "image" and part.get_content_disposition() == "inline":
        used.add(id(part))
        return [("image", part)]
    return []


def _attachments(msg, used: set, referenced: set) -> list[Attachment]:
    result = []
    for part in _leaves(msg):
        if id(part) in used:
            continue
        filename = part.get_filename()
        ctype = part.get_content_type()
        cid = (part.get("Content-ID") or "").strip().strip("<>")
        if ctype == "message/rfc822":
            try:
                data = part.get_content().as_bytes()
            except Exception:
                data = b""
            filename = filename or _("Attached Message") + ".eml"
        else:
            if cid and (cid in referenced or not filename):
                continue  # an image shown inside the HTML body
            if part.is_multipart():
                continue
            data = part.get_payload(decode=True) or b""
            if not data and not filename:
                continue
        result.append(Attachment(filename or _("Untitled Attachment"), ctype, data))
    return result


def _text_html(text: str) -> str:
    return ('<pre style="white-space:pre-wrap;font-family:inherit;font-size:inherit">'
            + html.escape(text) + "</pre>")


def parse_message(raw: bytes) -> MessageContent:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    inline = _inline_parts(msg)

    # Rich rendering (HTML preferred inside alternatives).
    used_html: set = set()
    html_segments = _collect(msg, "html", used_html)
    html_source = None
    if any(kind == "html" for kind, _value in html_segments):
        pieces = []
        for index, (kind, value) in enumerate(html_segments):
            if kind == "html":
                pieces.append(value)
            elif kind == "text":
                pieces.append(_text_html(value))
            else:
                cid = (value.get("Content-ID") or "").strip().strip("<>") or f"brev-inline-{index}"
                inline.setdefault(cid, (value.get_content_type(), value.get_payload(decode=True) or b""))
                pieces.append(f'<p><img src="cid:{html.escape(cid)}"></p>')
        html_source = "\n".join(pieces)

    # Text rendering (plain preferred inside alternatives); also used for quoting.
    used_text: set = set()
    text_segments = _collect(msg, "plain", used_text)  # never yields images
    has_plain = any(kind == "text" for kind, _value in text_segments)
    runs = []
    for kind, value in text_segments:
        if runs:
            _add(runs, "\n\n", PLAIN)
        if kind == "text":
            runs += plain_to_runs(value.strip("\n"))
        else:
            runs += html_to_runs(value)
    text = "".join(t for t, _s in runs)

    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS]
        runs = plain_to_runs(text) if has_plain else [(text, PLAIN)]
        runs.append(("\n\n" + _("[Message truncated]"), Style(italic=True)))

    referenced = set(re.findall(r"cid:([^\s\"'<>)]+)", html_source or "", re.I))
    return MessageContent(
        raw=raw,
        text=text,
        markup=runs_to_markup(runs),
        html=html_source,
        has_plain=has_plain,
        # Inline images only count as shown when there is an HTML rendering to show them in;
        # otherwise they are listed (with previews) as attachments.
        attachments=_attachments(msg, (used_html if html_source else set()) | used_text, referenced),
        inline=inline if html_source else {},
        remote_content=bool(html_source and _REMOTE_RE.search(html_source)),
    )


# --- HTML to plain text for the text/plain alternative of HTML mail ------------

class _HtmlToPlain(_HtmlToRuns):
    """Like the reader's converter, but keeps quote depth and link targets."""

    # Quotes are marked with "> " instead of spacing, so no blank lines around them.
    BLOCK_GAP = _HtmlToRuns.BLOCK_GAP - {"blockquote"}
    BLOCK = _HtmlToRuns.BLOCK | {"blockquote"}

    def _style(self):
        return Style(False, False, self.href, self.quote)  # "dim" carries the quote depth here

    def _break_style(self):
        return Style(False, False, None, self.quote)


def html_to_plain(markup: str) -> str:
    parser = _HtmlToPlain()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        pass
    out = []
    at_line_start = True
    for text, style in parser.runs:
        depth = style.dim if isinstance(style.dim, int) and not isinstance(style.dim, bool) else 0
        if style.href and text.strip() and text.strip() != style.href and not style.href.startswith("mailto:"):
            text = f"{text} <{style.href}>"
        pieces = text.split("\n")
        for index, piece in enumerate(pieces):
            if index:
                if at_line_start and depth:
                    out.append(">" + " >" * (depth - 1))  # an empty line inside a quote
                out.append("\n")
                at_line_start = True
            if piece:
                if at_line_start and depth:
                    out.append("> " * depth)
                out.append(piece)
                at_line_start = False
    # Keep leading blank lines: in a reply that is where the user writes.
    text = re.sub(r"\n{3,}", "\n\n", "".join(out)).rstrip("\n") + "\n"
    return re.sub(r"(?m)^--$", "-- ", text)  # keep the standard signature delimiter intact
