"""A small dialog listing every contact alphabetically."""

from __future__ import annotations

from gettext import gettext as _, ngettext

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, Pango  # noqa: E402

from .models import Address  # noqa: E402


class ContactRow(Gtk.ListBoxRow):
    def __init__(self, contact, on_write):
        super().__init__(activatable=True)
        self.contact = contact
        self.search_text = " ".join([contact.label, contact.org, *contact.emails, *contact.phones]).casefold()

        box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        box.append(Adw.Avatar(size=36, text=contact.label, show_initials=True))
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        labels.append(Gtk.Label(label=contact.label, xalign=0, ellipsize=Pango.EllipsizeMode.END))
        details = contact.emails or contact.phones or ([contact.org] if contact.org and contact.name else [])
        if details:
            labels.append(Gtk.Label(label=", ".join(details), xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                    css_classes=["dim-label", "caption"]))
        box.append(labels)
        if contact.emails:
            write = Gtk.Button(icon_name="mail-message-new-symbolic", valign=Gtk.Align.CENTER,
                               tooltip_text=_("New Message to {name}").format(name=contact.label),
                               css_classes=["flat", "circular"])
            write.connect("clicked", lambda _b: on_write(contact))
            box.append(write)
        self.set_child(box)
        self.update_property([Gtk.AccessibleProperty.LABEL], [", ".join([contact.label, *details])])


class ContactsDialog(Adw.Dialog):
    def __init__(self, window, book, on_refresh, is_online):
        super().__init__(title=_("Contacts"), content_width=420, content_height=620)
        self._window = window
        self._book = book
        self._on_refresh = on_refresh
        self._is_online = is_online

        self.title = Adw.WindowTitle(title=_("Contacts"))
        header = Adw.HeaderBar(title_widget=self.title)
        self.spinner = Adw.Spinner(visible=False, tooltip_text=_("Updating contacts…"))
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=_("Refresh Contacts"))
        refresh.connect("clicked", lambda _b: self._on_refresh())
        header.pack_start(refresh)
        header.pack_start(self.spinner)
        self.add_button = Gtk.Button(icon_name="list-add-symbolic", tooltip_text=_("Add Contact"))
        self.add_button.connect("clicked", lambda _b: self._edit(None))
        header.pack_end(self.add_button)

        self.search = Gtk.SearchEntry(placeholder_text=_("Search contacts"), hexpand=True,
                                      margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        self.search.connect("search-changed", lambda _e: self._refilter())

        self.list = Gtk.ListBox(css_classes=["navigation-sidebar"], selection_mode=Gtk.SelectionMode.NONE)
        self.list.set_header_func(self._header)
        self.list.set_filter_func(self._filter)
        self.list.connect("row-activated", lambda _l, row: self._edit(row.contact))
        scroller = Gtk.ScrolledWindow(child=self.list, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(scroller, "list")
        self.stack.add_named(Adw.StatusPage(
            icon_name="avatar-default-symbolic", title=_("No Contacts"),
            description=_("Turn on Contacts for an account in Settings → Online Accounts."),
            css_classes=["compact"]), "empty")
        self.stack.add_named(Adw.StatusPage(icon_name="system-search-symbolic", title=_("No Results"),
                                            css_classes=["compact"]), "no-results")

        toolbar = Adw.ToolbarView(content=self.stack)
        toolbar.add_top_bar(header)
        toolbar.add_top_bar(self.search)
        self.set_child(toolbar)
        self.set_focus(self.search)

        self._handler = book.connect("changed", lambda _b: self._populate())
        self.connect("closed", lambda _d: book.disconnect(self._handler))
        self._populate()

    def _edit(self, contact):
        from .contact_editor import ContactEditor
        ContactEditor(self._window, self._book, contact, self._is_online()).present(self)

    def _update_add_button(self):
        online = self._is_online()
        writable = bool(self._book.writable_sources())
        self.add_button.set_sensitive(online and writable)
        self.add_button.set_tooltip_text(
            _("Add Contact") if online and writable
            else _("You are offline") if not online
            else _("None of your accounts can store contacts"))

    def _populate(self):
        self._update_add_button()
        self.spinner.set_visible(self._book.refreshing)
        self.list.remove_all()
        for contact in self._book.contacts:
            self.list.append(ContactRow(contact, self._write))
        count = len(self._book.contacts)
        self.title.set_subtitle(ngettext("{n} contact", "{n} contacts", count).format(n=count) if count else "")
        self._refilter()

    def _refilter(self):
        self._query = self.search.get_text().strip().casefold()
        self.list.invalidate_filter()
        self.list.invalidate_headers()
        if not self._book.contacts:
            self.stack.set_visible_child_name("empty")
            return
        visible = any(self._filter(row) for row in self._rows())
        self.stack.set_visible_child_name("list" if visible else "no-results")

    def _rows(self):
        child = self.list.get_first_child()
        while child is not None:
            if isinstance(child, ContactRow):
                yield child
            child = child.get_next_sibling()

    def _filter(self, row):
        query = getattr(self, "_query", "")
        return all(word in row.search_text for word in query.split())

    def _header(self, row, before):
        if before is not None and before.contact.initial == row.contact.initial:
            row.set_header(None)
            return
        row.set_header(Gtk.Label(label=row.contact.initial, xalign=0, css_classes=["heading", "dim-label"],
                                 margin_start=12, margin_top=12 if before else 6, margin_bottom=4))

    def _write(self, contact):
        if not contact.emails:
            return
        self.close()
        self._window.compose(to=Address(contact.name, contact.emails[0]).format())
