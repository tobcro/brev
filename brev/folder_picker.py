"""A small dialog for picking the folder to move a message to."""

from __future__ import annotations

from gettext import gettext as _

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, Pango  # noqa: E402


class FolderPicker(Adw.Dialog):
    """Lists the folders of one account; calls on_pick(folder) for the one chosen."""

    def __init__(self, folders, on_pick, exclude=None):
        super().__init__(title=_("Move to Folder"), content_width=360, content_height=520)
        self._on_pick = on_pick

        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label=_("_Cancel"), use_underline=True)
        cancel.connect("clicked", lambda _b: self.close())
        header.pack_start(cancel)

        self.search = Gtk.SearchEntry(placeholder_text=_("Search folders"),
                                      margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        self.search.connect("search-changed", self._on_search_changed)
        self.search.connect("activate", self._on_search_activate)

        self.list = Gtk.ListBox(css_classes=["navigation-sidebar"], activate_on_single_click=True)
        self.list.update_property([Gtk.AccessibleProperty.LABEL], [_("Folders")])
        self.list.set_filter_func(self._filter)
        self.list.connect("row-activated", lambda _l, row: self._pick(row.folder))
        for folder in folders:
            if exclude is not None and folder.name == exclude.name:
                continue
            row = Gtk.ListBoxRow()
            row.folder = folder
            box = Gtk.Box(spacing=12, margin_start=folder.depth * 16)
            box.append(Gtk.Image(icon_name=folder.icon_name))
            box.append(Gtk.Label(label=folder.title, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END))
            row.set_child(box)
            self.list.append(row)

        self.empty = Adw.StatusPage(icon_name="folder-symbolic", title=_("No Matching Folders"),
                                    css_classes=["compact"], vexpand=True)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self.search)
        scroller = Gtk.ScrolledWindow(child=self.list, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.stack = Gtk.Stack()
        self.stack.add_named(scroller, "list")
        self.stack.add_named(self.empty, "empty")
        content.append(self.stack)

        view = Adw.ToolbarView(content=content)
        view.add_top_bar(header)
        self.set_child(view)
        self.set_focus(self.search)

    def _on_search_changed(self, _entry):
        self.list.invalidate_filter()
        self.stack.set_visible_child_name("list" if next(self._visible_rows(), None) else "empty")

    def _filter(self, row):
        text = self.search.get_text().strip().casefold()
        return not text or text in row.folder.title.casefold() or text in row.folder.name.casefold()

    def _visible_rows(self):
        row = self.list.get_first_child()
        while row is not None:
            if isinstance(row, Gtk.ListBoxRow) and row.get_child_visible():
                yield row
            row = row.get_next_sibling()

    def _on_search_activate(self, _entry):
        row = next(self._visible_rows(), None)
        if row is not None:
            self._pick(row.folder)

    def _pick(self, folder):
        self.close()
        self._on_pick(folder)
