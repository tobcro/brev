"""Folder rows, message rows and the message reader."""

from __future__ import annotations

import json
import os
import re
from gettext import gettext as _

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from . import html_view  # noqa: E402

from .models import Address  # noqa: E402
from .util import format_full_date, format_list_date  # noqa: E402


class FolderRow(Gtk.ListBoxRow):
    def __init__(self, service, folder, order):
        super().__init__()
        self.service = service
        self.folder = folder
        self.order = order

        box = Gtk.Box(spacing=12, margin_start=folder.depth * 16)
        box.append(Gtk.Image(icon_name=folder.icon_name))
        label = Gtk.Label(label=folder.title, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        box.append(label)
        self.count = Gtk.Label(css_classes=["unread-count", "numeric"])
        box.append(self.count)
        self.set_child(box)
        self.update()

    def update(self):
        unread = self.folder.unread if self.folder.counts_unread else 0
        self.count.set_label(str(unread) if unread else "")
        self.count.set_visible(unread > 0)


class MessageRow(Gtk.ListBoxRow):
    def __init__(self, summary, show_recipient=False):
        super().__init__(css_classes=["message-row"])
        self.summary = summary
        self.show_recipient = show_recipient

        grid = Gtk.Grid(column_spacing=8, row_spacing=3)
        self.dot = Gtk.Box(css_classes=["unread-dot"], valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        grid.attach(self.dot, 0, 0, 1, 1)

        self.sender = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        grid.attach(self.sender, 1, 0, 1, 1)
        self.date = Gtk.Label(xalign=1, css_classes=["dim-label", "caption", "numeric"])
        grid.attach(self.date, 2, 0, 1, 1)

        self.subject = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        grid.attach(self.subject, 1, 1, 1, 1)
        icons = Gtk.Box(spacing=4, halign=Gtk.Align.END)
        self.attachment = Gtk.Image(icon_name="mail-attachment-symbolic", css_classes=["dim-label"],
                                    tooltip_text=_("Has attachments"))
        self.star = Gtk.Image(icon_name="starred-symbolic", css_classes=["star"], tooltip_text=_("Starred"))
        icons.append(self.attachment)
        icons.append(self.star)
        grid.attach(icons, 2, 1, 1, 1)

        self.set_child(grid)
        self.update()

    def update(self):
        s = self.summary
        if self.show_recipient and s.to:
            who = _("To: {names}").format(names=", ".join(a.label for a in s.to))
        else:
            who = s.sender.label if s.sender else _("Unknown Sender")
        self.sender.set_label(who)
        self.subject.set_label(s.subject or _("(No Subject)"))
        self.date.set_label(format_list_date(s.timestamp))
        self.dot.set_opacity(1 if s.unread else 0)
        self.sender.set_css_classes(["sender"] + (["unread"] if s.unread else []))
        self.subject.set_css_classes(["subject"] + (["unread"] if s.unread else ["dim-label"]))
        self.attachment.set_visible(s.has_attachments)
        self.star.set_visible(s.flagged)
        self.update_property([Gtk.AccessibleProperty.LABEL], [
            ", ".join(filter(None, [
                _("Unread") if s.unread else None,
                _("Starred") if s.flagged else None,
                who, s.subject, format_list_date(s.timestamp),
            ]))
        ])


def _safe_filename(name: str) -> str:
    name = os.path.basename(name.replace("\\", "/")).strip().lstrip(".")
    return re.sub(r"[\x00-\x1f]", "", name) or "attachment"


_PREVIEW_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"}


class MessageView(Gtk.Stack):
    """Reader for one message: sandboxed HTML when available, otherwise linkified text."""

    def __init__(self, on_mailto, on_address=None, address_status=None):
        """on_address(action, Address) handles "add", "edit", "compose" and "copy" from the
        address menu; address_status(email) returns "known", "addable" or "other"."""
        super().__init__(transition_type=Gtk.StackTransitionType.NONE, vexpand=True)
        self._on_mailto = on_mailto
        self._on_address = on_address
        self._address_status = address_status or (lambda _email: "other")
        self.mode = "text"
        self._setup_address_actions()

        # Headers shared by both pages; moved to whichever page is showing.
        self.header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.subject = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                                 selectable=True, css_classes=["title-2"])
        self.header.append(self.subject)

        row = Gtk.Box(spacing=12)
        self.avatar = Adw.Avatar(size=42, show_initials=True, valign=Gtk.Align.START)
        row.append(self.avatar)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        # The sender is a button: click for Add to Contacts / Edit Contact / New Message / Copy.
        sender_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.sender_name = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                                     css_classes=["heading"])
        self.sender_email = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                                      css_classes=["dim-label", "caption"])
        sender_box.append(self.sender_name)
        sender_box.append(self.sender_email)
        self.sender_button = Gtk.Button(child=sender_box, halign=Gtk.Align.START,
                                        css_classes=["flat", "sender-button"])
        self.sender_button.connect("clicked", lambda b: self._sender and self._show_address_menu(b, self._sender))
        self._sender = None
        info.append(self.sender_button)
        self.recipients = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info.append(self.recipients)
        row.append(info)
        self.date = Gtk.Label(xalign=1, valign=Gtk.Align.START, css_classes=["dim-label", "caption"])
        row.append(self.date)
        self.header.append(row)

        self.attachments = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self.header.append(self.attachments)
        self.previews = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=4,
                                    column_spacing=8, row_spacing=8, homogeneous=True)
        self.header.append(self.previews)

        # Text page: everything scrolls together.
        self.text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                                margin_top=18, margin_bottom=36, margin_start=18, margin_end=18)
        self.text_box.append(self.header)
        self.text_box.append(Gtk.Separator())
        self.body = Gtk.Label(xalign=0, yalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                              selectable=True, valign=Gtk.Align.START, css_classes=["message-body"])
        self.body.connect("activate-link", self._on_activate_link)
        self.text_box.append(self.body)
        self.text_scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True,
            child=self.text_box)
        self.add_named(self.text_scroller, "text")

        # HTML page: headers on top, the web view scrolls the message itself.
        self.html_view = None
        if html_view.AVAILABLE:
            self.html_header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                       margin_top=18, margin_bottom=12, margin_start=18, margin_end=18)
            self.html_view = html_view.HtmlView(on_link=self._open_uri)
            self.html_view.set_margin_start(12)
            self.html_view.set_margin_end(12)
            self.html_view.set_margin_bottom(12)
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            page.append(self.html_header)
            page.append(self.html_view)
            self.add_named(page, "html")

    def _move_header(self, target):
        parent = self.header.get_parent()
        if parent is target:
            return
        if parent is not None:
            parent.remove(self.header)
        target.prepend(self.header)

    def show(self, summary, content, prefer_html=True, allow_remote=False) -> str:
        """Show a message. Returns the mode used: "html" or "text"."""
        self.subject.set_label(summary.subject or _("(No Subject)"))
        sender = summary.sender
        self.sender_name.set_label(sender.label if sender else _("Unknown Sender"))
        self.sender_email.set_label(sender.email if sender and sender.name else "")
        self.sender_email.set_visible(bool(sender and sender.name))
        self.avatar.set_text(sender.label if sender else "")

        self._sender = sender
        self.sender_button.set_sensitive(sender is not None and bool(sender.email))
        self.sender_button.set_tooltip_text(_("Contact options for {address}").format(address=sender.email)
                                            if sender and sender.email else None)

        while (child := self.recipients.get_first_child()) is not None:
            self.recipients.remove(child)
        for caption, addresses in ((_("To:"), summary.to), (_("Cc:"), summary.cc)):
            if addresses:
                self.recipients.append(self._address_line(caption, addresses))
        self.recipients.set_visible(bool(summary.to or summary.cc))
        self.date.set_label(format_full_date(summary.timestamp))

        self.attachments.remove_all()
        for attachment in content.attachments:
            self.attachments.append(self._attachment_row(attachment))
        self.attachments.set_visible(bool(content.attachments))
        self._show_previews(content.attachments)

        if self.html_view is not None and content.html and prefer_html:
            self.mode = "html"
            self.body.set_text("")  # don't keep the previous text message around, even hidden
            self._move_header(self.html_header)
            self.html_view.show_html(content.html, content.inline, allow_remote)
            self.set_visible_child_name("html")
            return self.mode

        self.mode = "text"
        self._move_header(self.text_box)
        if self.html_view is not None:
            self.html_view.clear()
        try:
            # Pango does not know GtkLabel's <a> extension, so validate without it.
            Pango.parse_markup(re.sub(r"</?a\b[^>]*>", "", content.markup), -1, "\0")
            self.body.set_markup(content.markup)
        except GLib.Error:
            self.body.set_text(content.text)
        if not content.text.strip() and not content.attachments:
            self.body.set_markup("<i>" + GLib.markup_escape_text(_("This message has no text.")) + "</i>")
        self.body.select_region(0, 0)
        self.set_visible_child_name("text")
        self.text_scroller.get_vadjustment().set_value(0)
        return self.mode

    # --- clickable addresses ------------------------------------------------

    _MAX_SHOWN = 25

    def _address_line(self, caption, addresses):
        line = Adw.WrapBox(child_spacing=2, line_spacing=2)
        line.append(Gtk.Label(label=caption, css_classes=["dim-label", "caption"], margin_end=4))
        for address in addresses[:self._MAX_SHOWN]:
            button = Gtk.Button(label=address.label, css_classes=["flat", "address-chip", "caption"],
                                tooltip_text=address.email)
            button.connect("clicked", lambda b, a=address: self._show_address_menu(b, a))
            line.append(button)
        hidden = len(addresses) - self._MAX_SHOWN
        if hidden > 0:
            line.append(Gtk.Label(label=_("+{n} more").format(n=hidden), css_classes=["dim-label", "caption"],
                                  tooltip_text=", ".join(a.email for a in addresses[self._MAX_SHOWN:])))
        return line

    def _setup_address_actions(self):
        group = Gio.SimpleActionGroup()
        for name in ("add", "edit", "compose", "copy"):
            action = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            action.connect("activate", self._on_address_action, name)
            group.add_action(action)
        self.insert_action_group("address", group)

    def _on_address_action(self, _action, parameter, name):
        if self._on_address is not None:
            display, email = json.loads(parameter.get_string())
            self._on_address(name, Address(display, email))

    def _show_address_menu(self, button, address):
        target = GLib.Variant("s", json.dumps([address.name, address.email]))
        status = self._address_status(address.email)
        contact = Gio.Menu()
        if status == "known":
            contact.append_item(self._item(_("Edit Contact"), "address.edit", target))
        elif status == "addable":
            contact.append_item(self._item(_("Add to Contacts"), "address.add", target))
        other = Gio.Menu()
        other.append_item(self._item(_("New Message"), "address.compose", target))
        other.append_item(self._item(_("Copy Address"), "address.copy", target))
        menu = Gio.Menu()
        menu.append_section(address.email, contact)
        menu.append_section(None, other)
        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(button)
        popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
        popover.popup()

    @staticmethod
    def _item(label, action, target):
        item = Gio.MenuItem.new(label, None)
        item.set_action_and_target_value(action, target)
        return item

    def _show_previews(self, attachments):
        self.previews.remove_all()
        shown = 0
        for attachment in attachments:
            if attachment.content_type not in _PREVIEW_TYPES or attachment.size > 20 * 1024 * 1024 or shown >= 8:
                continue
            try:
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(attachment.data))
            except GLib.Error:
                continue
            picture = Gtk.Picture(paintable=texture, content_fit=Gtk.ContentFit.COVER, can_shrink=True)
            picture.set_size_request(-1, 120)
            button = Gtk.Button(child=picture, css_classes=["flat", "attachment-preview"],
                                tooltip_text=attachment.filename, overflow=Gtk.Overflow.HIDDEN)
            button.connect("clicked", lambda _b, a=attachment: self._open_attachment(a))
            self.previews.append(button)
            shown += 1
        self.previews.set_visible(shown > 0)

    # --- links and attachments ---------------------------------------

    def _on_activate_link(self, _label, uri):
        self._open_uri(uri)
        return True

    def _open_uri(self, uri):
        if uri.lower().startswith("mailto:"):
            self._on_mailto(uri)
        else:
            Gtk.UriLauncher.new(uri).launch(self.get_root(), None, None)

    def _attachment_row(self, attachment):
        description = Gio.content_type_get_description(attachment.content_type)
        row = Adw.ActionRow(title=attachment.filename, use_markup=False,
                            subtitle=f"{GLib.format_size(attachment.size)} · {description}",
                            activatable=True)
        row.add_prefix(Gtk.Image(gicon=Gio.content_type_get_symbolic_icon(attachment.content_type)))
        save = Gtk.Button(icon_name="document-save-symbolic", tooltip_text=_("Save As…"),
                          valign=Gtk.Align.CENTER, css_classes=["flat"])
        save.connect("clicked", lambda _b: self._save_attachment(attachment))
        row.add_suffix(save)
        row.connect("activated", lambda _r: self._open_attachment(attachment))
        row.set_tooltip_text(_("Open"))
        return row

    def _open_attachment(self, attachment):
        folder = os.path.join(GLib.get_user_cache_dir(), "brev", "attachments")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, _safe_filename(attachment.filename))
        with open(path, "wb") as handle:
            handle.write(attachment.data)
        Gtk.FileLauncher.new(Gio.File.new_for_path(path)).launch(self.get_root(), None, None)

    def _save_attachment(self, attachment):
        dialog = Gtk.FileDialog(initial_name=_safe_filename(attachment.filename))

        def finished(dialog, result):
            try:
                target = dialog.save_finish(result)
            except GLib.Error:
                return  # cancelled
            target.replace_contents(attachment.data, None, False,
                                    Gio.FileCreateFlags.REPLACE_DESTINATION, None)

        dialog.save(self.get_root(), None, finished)
