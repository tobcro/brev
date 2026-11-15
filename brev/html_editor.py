"""A rich-text (HTML) message editor built on a contenteditable WebKit view."""

from __future__ import annotations

import base64
import html
import json
import logging
import re
from gettext import gettext as _

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from .html_view import AVAILABLE, WebKit  # noqa: E402

log = logging.getLogger(__name__)

_WORLD = "brev"

# Page scripts stay off; this policy additionally keeps the editor from loading anything
# from the network (pasted remote images simply do not show).
_CSP = "default-src 'none'; img-src data: cid:; style-src 'unsafe-inline'"

EDITOR_CSS = """
html, body { height: 100%; }
body { margin: 0; padding: 12px; box-sizing: border-box; font-family: sans-serif; font-size: 15px;
       line-height: 1.45; color: #1e1e21; background: #ffffff; outline: none; overflow-wrap: break-word; }
blockquote { margin: 0 0 0 .8ex; padding-left: 1ex; border-left: 2px solid #c0bfbc; color: #5e5c64; }
a { color: #1c71d8; }
img { max-width: 100%; }
"""

# Run in an isolated world: report edits, and read the content back out.
_WATCH_JS = """
if (!window.__brevWatching) {
  window.__brevWatching = true;
  document.body.addEventListener('input', function () {
    window.webkit.messageHandlers.brevInput.postMessage(1);
  });
}
"""
_CONTENT_JS = "JSON.stringify({html: document.body.innerHTML})"
# A command with no text cursor in the editor would do nothing: put one at the start first.
_ENSURE_CARET_JS = """
if (!window.getSelection().rangeCount) {
  var r = document.createRange(); r.setStart(document.body, 0); r.collapse(true);
  window.getSelection().addRange(r);
}
"""
_FOCUS_END_JS = """
document.body.focus();
var range = document.createRange(); range.setStart(document.body, 0); range.collapse(true);
var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
"""


def text_to_html(text: str) -> str:
    """Plain text to editor HTML. Lines starting with '>' become (nested) blockquotes."""
    lines = text.split("\n")
    out = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith(">"):
            block = []
            while index < len(lines) and lines[index].startswith(">"):
                inner = lines[index][1:]
                block.append(inner[1:] if inner.startswith(" ") else inner)
                index += 1
            out.append("<blockquote>" + text_to_html("\n".join(block)) + "</blockquote>")
            continue
        out.append("<div>" + (html.escape(line) or "<br>") + "</div>")
        index += 1
    return "".join(out)


class HtmlEditor(Gtk.Box):
    """Emits "changed" when the user edits. Content is read asynchronously."""

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, initial_html: str = ""):
        super().__init__(css_classes=["html-card"], overflow=Gtk.Overflow.HIDDEN, vexpand=True, hexpand=True)
        manager = WebKit.UserContentManager()
        manager.register_script_message_handler("brevInput", _WORLD)
        manager.connect("script-message-received::brevInput", lambda *_a: self.emit("changed"))
        settings = WebKit.Settings(
            enable_javascript_markup=False, enable_developer_extras=False,
            enable_html5_local_storage=False, enable_html5_database=False,
            enable_webgl=False, enable_webaudio=False, enable_media=False,
            enable_back_forward_navigation_gestures=False,
        )
        self.web = WebKit.WebView(network_session=WebKit.NetworkSession.new_ephemeral(),
                                  user_content_manager=manager, settings=settings,
                                  hexpand=True, vexpand=True, editable=False)
        self.web.set_background_color(Gdk.RGBA(red=1, green=1, blue=1, alpha=1))
        self.web.connect("load-changed", self._on_load_changed)
        self.web.connect("decide-policy", self._on_decide_policy)
        self.web.update_property([Gtk.AccessibleProperty.LABEL], ["Message"])
        self.append(self.web)

        # Pictures on the clipboard: WebKit cannot paste them into a message by itself.
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key_pressed)
        self.web.add_controller(keys)
        self.web.connect("context-menu", self._on_context_menu)
        self._paste_action = Gio.SimpleAction.new("paste-image", None)
        self._paste_action.connect("activate", lambda *_a: self.paste_image())
        self._ready = False
        self._pending_focus = False
        self.set_html(initial_html)

    def set_html(self, body_html: str):
        self._ready = False
        document = ('<!doctype html><meta charset="utf-8">'
                    f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">'
                    f"<style>{EDITOR_CSS}</style>"
                    f'<body contenteditable="true">{body_html}</body>')
        self.web.load_html(document, "about:blank")

    def _on_load_changed(self, web, event):
        if event == WebKit.LoadEvent.FINISHED:
            self._ready = True
            web.evaluate_javascript(_WATCH_JS, -1, _WORLD, None, None, None)
            if self._pending_focus:
                self.focus_start()

    def _on_decide_policy(self, _web, decision, kind):
        # The editor never navigates: clicking a link while editing must not leave the message.
        if kind == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri() or ""
            if uri == "about:blank":
                decision.use()
            else:
                decision.ignore()
            return True
        if kind == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION:
            decision.ignore()
            return True
        return False

    def focus_start(self):
        """Focus the editor with the cursor at the very beginning (above any signature or quote)."""
        if not self._ready:
            self._pending_focus = True
            return
        self._pending_focus = False
        self.web.grab_focus()
        self.web.evaluate_javascript(_FOCUS_END_JS, -1, _WORLD, None, None, None)

    def command(self, name: str, argument: str | None = None):
        """Run an editing command such as Bold, Italic, Underline, InsertUnorderedList, CreateLink.

        Runs inside the page (on the document's own selection), so it works even when a toolbar
        button has just taken keyboard focus away from the editor.
        """
        self.run_script(_ENSURE_CARET_JS + "document.execCommand(%s, false, %s);"
                        % (json.dumps(name), json.dumps(argument)))
        self.web.grab_focus()
        self.emit("changed")

    def run_script(self, script: str):
        self.web.evaluate_javascript(script, -1, _WORLD, None, None, None)

    # --- pictures -----------------------------------------------------------

    def insert_image(self, data: bytes, mime: str, name: str = ""):
        """Insert a picture at the cursor. It is sent as an embedded (cid:) part."""
        uri = "data:%s;base64,%s" % (mime, base64.b64encode(data).decode())
        markup = '<img src="%s" alt="%s" style="max-width:100%%;height:auto">' % (uri, html.escape(name, quote=True))
        self.run_script(_ENSURE_CARET_JS + "document.execCommand('insertHTML', false, %s);" % json.dumps(markup))
        self.web.grab_focus()
        self.emit("changed")

    def clipboard_has_image(self) -> bool:
        formats = self.get_clipboard().get_formats()
        texture = Gdk.Texture.__gtype__
        if any(GObject.type_is_a(gtype, texture) for gtype in (formats.get_gtypes() or [])):
            return True  # copied inside a GTK app (reported as a subclass such as GdkMemoryTexture)
        return any(t.startswith("image/") for t in (formats.get_mime_types() or []))  # from other apps

    def paste_image(self):
        def finished(clipboard, result):
            try:
                texture = clipboard.read_texture_finish(result)
            except GLib.Error as err:
                log.info("No picture on the clipboard: %s", err.message)
                return
            if texture is not None:
                self.insert_image(texture.save_to_png_bytes().get_data(), "image/png", "Pasted image")

        self.get_clipboard().read_texture_async(None, finished)

    def _on_key_pressed(self, _controller, keyval, _keycode, state):
        ctrl = state & Gdk.ModifierType.CONTROL_MASK
        shift = state & Gdk.ModifierType.SHIFT_MASK
        is_paste = (ctrl and not shift and keyval in (Gdk.KEY_v, Gdk.KEY_V)) or (shift and keyval == Gdk.KEY_Insert)
        if is_paste and self.clipboard_has_image():
            self.paste_image()
            return True  # handled; text pastes go to WebKit as usual
        return False

    def _on_context_menu(self, _web, menu, _hit):
        if not self.clipboard_has_image():
            return False
        for index, item in enumerate(list(menu.get_items())):
            if item.get_stock_action() == WebKit.ContextMenuAction.PASTE:
                menu.remove(item)
                menu.insert(WebKit.ContextMenuItem.new_from_gaction(self._paste_action, _("Paste"), None), index)
                break
        return False

    def get_html(self, callback):
        """callback(body_html) with the current content (or None if it could not be read)."""
        def finished(web, result):
            try:
                value = web.evaluate_javascript_finish(result).to_string()
                callback(json.loads(value)["html"])
            except (GLib.Error, ValueError, KeyError) as err:
                log.warning("Could not read the message editor: %s", err)
                callback(None)

        self.web.evaluate_javascript(_CONTENT_JS, -1, _WORLD, None, None, finished)


def available() -> bool:
    return AVAILABLE


_DATA_IMAGE_RE = re.compile(r"""src=(["'])data:(image/[\w.+-]+);base64,([A-Za-z0-9+/=\s]+)\1""", re.I)


def extract_inline_images(body_html: str, make_cid):
    """Turn pasted data: images into cid: references. Returns (html, [(cid, mime, bytes)])."""
    import base64
    images = []

    def replace(match):
        cid = make_cid()
        try:
            data = base64.b64decode(re.sub(r"\s", "", match.group(3)))
        except ValueError:
            return match.group(0)
        images.append((cid, match.group(2).lower(), data))
        return f'src="cid:{cid}"'

    return _DATA_IMAGE_RE.sub(replace, body_html), images
