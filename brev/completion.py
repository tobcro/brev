"""Address suggestions for the recipient fields of the compose dialog."""

from __future__ import annotations

from email.utils import getaddresses

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk, Pango  # noqa: E402

from .models import Address  # noqa: E402


class AddressCompleter:
    """Shows contacts matching the address being typed under an Adw.EntryRow.

    Up/Down choose, Enter or Tab accepts, Escape closes. Several comma-separated
    addresses are supported; only the one at the cursor is completed.
    """

    def __init__(self, row: Adw.EntryRow, book):
        self.row = row
        self.book = book
        self._updating = False

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, css_classes=["navigation-sidebar"])
        self.list.connect("row-activated", lambda _l, r: self._accept(r))
        scroller = Gtk.ScrolledWindow(child=self.list, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      propagate_natural_height=True, max_content_height=320)
        self.popover = Gtk.Popover(child=scroller, autohide=False, has_arrow=False,
                                   position=Gtk.PositionType.BOTTOM, can_focus=False,
                                   css_classes=["menu", "address-suggestions"])
        self.popover.set_parent(row)

        row.connect("changed", self._on_changed)
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        row.add_controller(keys)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_a: self.hide())
        row.add_controller(focus)

    def destroy(self):
        self.popover.unparent()

    # --- current token ---------------------------------------------------

    def _token(self):
        text = self.row.get_text()
        cursor = self.row.get_position()
        start = max(text.rfind(",", 0, cursor), text.rfind(";", 0, cursor)) + 1
        end_match = [i for i in (text.find(",", cursor), text.find(";", cursor)) if i != -1]
        end = min(end_match) if end_match else len(text)
        return text, start, end, text[start:cursor].strip()

    def _on_changed(self, _row):
        if self._updating:
            return
        text, start, _end, token = self._token()
        if not token or "<" in token:
            self.hide()
            return
        others = [addr for _name, addr in getaddresses([text[:start]])]
        matches = self.book.suggest(token, exclude=others)
        self.list.remove_all()
        for name, email in matches:
            self.list.append(self._suggestion_row(name, email))
        if not matches:
            self.hide()
            return
        self.list.select_row(self.list.get_row_at_index(0))
        self.popover.set_size_request(max(self.row.get_width() - 24, 240), -1)
        self.popover.set_offset(0, 2)
        self.popover.popup()

    def _suggestion_row(self, name, email):
        box = Gtk.Box(spacing=10, margin_top=2, margin_bottom=2)
        box.append(Adw.Avatar(size=28, text=name or email, show_initials=True))
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        labels.append(Gtk.Label(label=name or email, xalign=0, ellipsize=Pango.EllipsizeMode.END))
        if name:
            labels.append(Gtk.Label(label=email, xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                    css_classes=["dim-label", "caption"]))
        box.append(labels)
        row = Gtk.ListBoxRow(child=box)
        row.address = Address(name, email)
        return row

    # --- keyboard ----------------------------------------------------------

    def _on_key(self, _controller, keyval, _keycode, _state):
        if not self.popover.get_visible():
            return False
        selected = self.list.get_selected_row()
        index = selected.get_index() if selected else -1
        if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            target = self.list.get_row_at_index(index + 1)
        elif keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            target = self.list.get_row_at_index(max(index - 1, 0))
        elif keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Tab, Gdk.KEY_ISO_Enter):
            if selected:
                self._accept(selected)
                return True
            return False
        elif keyval == Gdk.KEY_Escape:
            self.hide()
            return True
        else:
            return False
        if target:
            self.list.select_row(target)  # selection only; keyboard focus stays in the entry
        return True

    def _accept(self, suggestion):
        text, start, end, _token = self._token()
        before = text[:start].rstrip()
        after = text[end:].lstrip(",; ")
        inserted = suggestion.address.format() + ", "
        prefix = before + (" " if before else "")
        new_text = prefix + inserted + after
        self._updating = True
        self.row.set_text(new_text)
        self.row.set_position(len(prefix + inserted))
        self._updating = False
        self.hide()

    def hide(self):
        if self.popover.get_visible():
            self.popover.popdown()
