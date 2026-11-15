"""Sandboxed HTML rendering for messages (WebKitGTK 6)."""

from __future__ import annotations

import logging
import re
import urllib.parse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit  # noqa: E402
    AVAILABLE = True
except (ValueError, ImportError):
    WebKit = None
    AVAILABLE = False

log = logging.getLogger(__name__)

# No scripts, frames, forms or network access; only images, styles and fonts that are
# embedded in the message itself. "Show Images" swaps in the second policy.
_STRICT = ("default-src 'none'; img-src cid: data:; style-src 'unsafe-inline' cid: data:; "
           "font-src cid: data:; media-src cid: data:; form-action 'none'")
_REMOTE = ("default-src 'none'; img-src * cid: data:; style-src * 'unsafe-inline' cid: data:; "
           "font-src * cid: data:; media-src * cid: data:; form-action 'none'")

# Kept minimal: author CSS outranks the message's own presentational attributes.
_BASE_CSS = ("html{color-scheme:light}body{overflow-wrap:break-word}"
             "img{max-width:100%}pre{white-space:pre-wrap}")

_KEEP_MENU_ITEMS = set()
if AVAILABLE:
    _KEEP_MENU_ITEMS = {
        WebKit.ContextMenuAction.COPY,
        WebKit.ContextMenuAction.COPY_LINK_TO_CLIPBOARD,
        WebKit.ContextMenuAction.COPY_IMAGE_TO_CLIPBOARD,
        WebKit.ContextMenuAction.COPY_IMAGE_URL_TO_CLIPBOARD,
    }


# Read in an isolated world (not subject to the page's CSP, invisible to the page):
# which load does the document now in the view belong to?
_SERIAL_JS = 'parseInt((document.querySelector(\'meta[name="brev-load"]\') || {content: "-1"}).content, 10)'
_WORLD = "brev"
_REVEAL_FALLBACK_MS = 1200


def build_document(html_source: str, allow_remote: bool, serial: int = 0) -> str:
    body = re.sub(r"^\s*<!doctype[^>]*>", "", html_source, flags=re.I)
    csp = _REMOTE if allow_remote else _STRICT
    return ('<!doctype html><meta charset="utf-8">'
            f'<meta http-equiv="Content-Security-Policy" content="{csp}">'
            f'<meta name="brev-load" content="{serial}">'
            f"<style>{_BASE_CSS}</style>" + body)


class HtmlView(Gtk.Box):
    """A WebView showing one message's HTML, drawn on a white card."""

    _active = None  # the view whose message answers cid: requests

    def __init__(self, on_link):
        super().__init__(css_classes=["html-card"], overflow=Gtk.Overflow.HIDDEN, vexpand=True, hexpand=True)
        self._on_link = on_link
        self._inline = {}
        _register_cid_scheme()

        settings = WebKit.Settings(
            enable_javascript_markup=False, enable_developer_extras=False,
            enable_html5_local_storage=False, enable_html5_database=False,
            enable_webgl=False, enable_webaudio=False, enable_media=False,
            enable_page_cache=False, enable_back_forward_navigation_gestures=False,
            default_font_family="sans-serif",
        )
        font = Gtk.Settings.get_default().props.gtk_font_name or ""
        family = re.sub(r"\s+\d+(\.\d+)?$", "", font)
        if family:
            settings.set_sans_serif_font_family(family)
        self._serial = 0
        self.web = WebKit.WebView(network_session=WebKit.NetworkSession.new_ephemeral(),
                                  settings=settings, hexpand=True, vexpand=True)
        self.web.connect("load-changed", self._on_load_changed)
        self.web.set_background_color(Gdk.RGBA(red=1, green=1, blue=1, alpha=1))
        self.web.connect("decide-policy", self._on_decide_policy)
        self.web.connect("context-menu", self._on_context_menu)
        self.append(self.web)

    def show_html(self, html_source: str, inline: dict, allow_remote: bool):
        """Load a message. The view stays blank until that very document has painted,
        so the previous message can never show through under the new headers."""
        self._serial += 1
        serial = self._serial
        self._inline = inline
        HtmlView._active = self
        self.web.set_opacity(0)
        GLib.timeout_add(_REVEAL_FALLBACK_MS, lambda: self._reveal(serial) and False)
        self.web.load_html(build_document(html_source, allow_remote, serial), "about:blank")

    def clear(self):
        self._serial += 1
        self._inline = {}
        self.web.set_opacity(0)
        self.web.load_html("", "about:blank")

    def _on_load_changed(self, web, event):
        if event == WebKit.LoadEvent.FINISHED:
            web.evaluate_javascript(_SERIAL_JS, -1, _WORLD, None, None, self._on_serial)

    def _on_serial(self, web, result):
        try:
            serial = int(web.evaluate_javascript_finish(result).to_double())
        except GLib.Error:
            return
        if serial != self._serial:
            return  # a load that has since been replaced
        frames = [0]

        def tick(_widget, _clock):
            frames[0] += 1
            if frames[0] < 2:
                return GLib.SOURCE_CONTINUE  # let WebKit present the new document first
            self._reveal(serial)
            return GLib.SOURCE_REMOVE

        self.web.add_tick_callback(tick)

    def _reveal(self, serial):
        if serial == self._serial:  # ignore signals from loads that were replaced
            self.web.set_opacity(1)
        return False

    def lookup(self, cid: str):
        return self._inline.get(cid)

    # --- policy -----------------------------------------------------------

    def _on_decide_policy(self, _web, decision, kind):
        if kind not in (WebKit.PolicyDecisionType.NAVIGATION_ACTION, WebKit.PolicyDecisionType.NEW_WINDOW_ACTION):
            return False
        action = decision.get_navigation_action()
        uri = action.get_request().get_uri() or ""
        if uri.startswith("about:blank#") and kind == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            decision.use()  # jump within the message
            return True
        clicked = (kind == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION
                   or action.get_navigation_type() == WebKit.NavigationType.LINK_CLICKED)
        if clicked:
            decision.ignore()
            if re.match(r"(https?|mailto):", uri, re.I):
                self._on_link(uri)
            return True
        if uri in ("about:blank", ""):
            decision.use()
            return True
        decision.ignore()  # meta refresh, form posts and the like
        return True

    def _on_context_menu(self, _web, menu, _hit):
        for item in list(menu.get_items()):
            if item.get_stock_action() not in _KEEP_MENU_ITEMS:
                menu.remove(item)
        return menu.get_n_items() == 0  # True suppresses an empty menu


_scheme_registered = False


def _register_cid_scheme():
    global _scheme_registered
    if _scheme_registered:
        return
    context = WebKit.WebContext.get_default()
    context.register_uri_scheme("cid", _on_cid_request)
    _scheme_registered = True


def _on_cid_request(request):
    cid = urllib.parse.unquote(request.get_path() or "")
    view = HtmlView._active
    found = view.lookup(cid) if view is not None else None
    if found is None:
        request.finish_error(GLib.Error.new_literal(Gio.io_error_quark(), "Not found", Gio.IOErrorEnum.NOT_FOUND))
        return
    content_type, data = found
    stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(data))
    request.finish(stream, len(data), content_type)
