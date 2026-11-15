"""The compose dialog."""

from __future__ import annotations

import html
import json
import re
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid
from gettext import gettext as _

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from .completion import AddressCompleter  # noqa: E402
from .html_editor import HtmlEditor, available as html_available, extract_inline_images, text_to_html  # noqa: E402
from .message import Attachment, html_to_plain  # noqa: E402
from .util import describe_error  # noqa: E402


def _parse_recipients(text: str):
    """Return (addresses, invalid entries)."""
    pairs = getaddresses([text.replace(";", ",")])
    good, bad = [], []
    for name, addr in pairs:
        if not addr and not name:
            continue
        if "@" in addr and " " not in addr and not addr.startswith("@") and not addr.endswith("@"):
            good.append((name, addr))
        else:
            bad.append(addr or name)
    return good, bad


_WEB_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def _format_pairs(pairs):
    return ", ".join(f"{name} <{addr}>" if name else addr for name, addr in pairs)


class ComposeDialog(Adw.Dialog):
    def __init__(self, window, services, service, *, to="", cc="", subject="", body="",
                 in_reply_to="", references="", attachments=(), title=None, body_format=None):
        super().__init__(title=title or _("New Message"), content_width=700, content_height=680)
        self._window = window
        self._services = services
        self._service = service
        self._in_reply_to = in_reply_to
        self._references = references
        self._attachments: list[Attachment] = []
        self._dirty = False

        header = Adw.HeaderBar()
        self.send_button = Gtk.Button(label=_("_Send"), use_underline=True, css_classes=["suggested-action"],
                                      tooltip_text=_("Send (Ctrl+Enter)"))
        self.send_button.connect("clicked", lambda _b: self._send())
        header.pack_end(self.send_button)
        attach = Gtk.Button(icon_name="mail-attachment-symbolic", tooltip_text=_("Attach Files…"))
        attach.connect("clicked", lambda _b: self._choose_files())
        header.pack_start(attach)
        self.spinner = Adw.Spinner(visible=False)
        header.pack_end(self.spinner)

        fields = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self.from_row = None
        if len(services) > 1:
            names = Gtk.StringList.new([s.account.title for s in services])
            self.from_row = Adw.ComboRow(title=_("From"), model=names, selected=services.index(service))
            self.from_row.connect("notify::selected", self._on_from_changed)
            fields.append(self.from_row)

        self.to_row = Adw.EntryRow(title=_("To"), text=to)
        cc_toggle = Gtk.ToggleButton(label=_("Cc/Bcc"), valign=Gtk.Align.CENTER, css_classes=["flat"])
        self.to_row.add_suffix(cc_toggle)
        self.cc_row = Adw.EntryRow(title=_("Cc"), text=cc)
        self.bcc_row = Adw.EntryRow(title=_("Bcc"))
        for row in (self.cc_row, self.bcc_row):
            cc_toggle.bind_property("active", row, "visible", GObject.BindingFlags.SYNC_CREATE)
        cc_toggle.set_active(bool(cc))
        self.subject_row = Adw.EntryRow(title=_("Subject"), text=subject)
        for row in (self.to_row, self.cc_row, self.bcc_row, self.subject_row):
            fields.append(row)
            row.connect("changed", self._mark_dirty)
            row.connect("entry-activated", lambda _r: self.text_view.grab_focus())

        self.attachment_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                           css_classes=["boxed-list"], visible=False)

        self.text_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, vexpand=True,
                                      top_margin=12, bottom_margin=12, left_margin=12, right_margin=12,
                                      css_classes=["compose-body"])
        self.text_view.update_property([Gtk.AccessibleProperty.LABEL], [_("Message")])
        buffer = self.text_view.get_buffer()
        buffer.set_text(body)
        buffer.place_cursor(buffer.get_start_iter())
        buffer.connect("changed", self._mark_dirty)
        scroller = Gtk.ScrolledWindow(child=self.text_view, vexpand=True,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER)
        frame = Gtk.Frame(child=scroller)

        # Body: plain text, or HTML in a WebKit editor (created when first needed).
        self.body_stack = Gtk.Stack(vexpand=True)
        self.body_stack.add_named(frame, "plain")
        self.editor = None
        self._format = body_format or window.get_application().settings.compose_format
        if self._format not in ("plain", "html") or not html_available():
            self._format = "plain"
        self._reverting = False

        self.format_group = Adw.ToggleGroup(valign=Gtk.Align.CENTER, sensitive=html_available())
        self.format_group.add(Adw.Toggle(name="plain", label=_("Text"), tooltip=_("Plain Text")))
        self.format_group.add(Adw.Toggle(name="html", label=_("HTML"), tooltip=_("Formatted Text (HTML)")))
        self.format_group.set_active_name(self._format)
        self.format_group.connect("notify::active-name", self._on_format_toggled)
        if not html_available():
            self.format_group.set_tooltip_text(_("HTML needs WebKitGTK 6 (gir1.2-webkit-6.0)"))
        self.format_tools = Gtk.Box(spacing=2)
        for icon, tip, command in (
            ("format-text-bold-symbolic", _("Bold (Ctrl+B)"), "Bold"),
            ("format-text-italic-symbolic", _("Italic (Ctrl+I)"), "Italic"),
            ("format-text-underline-symbolic", _("Underline (Ctrl+U)"), "Underline"),
            ("view-list-bullet-symbolic", _("Bulleted List"), "InsertUnorderedList"),
            ("view-list-ordered-symbolic", _("Numbered List"), "InsertOrderedList"),
        ):
            button = Gtk.Button(icon_name=icon, tooltip_text=tip, css_classes=["flat"])
            button.connect("clicked", lambda _b, c=command: self.editor and self.editor.command(c))
            self.format_tools.append(button)
        link = Gtk.Button(icon_name="insert-link-symbolic", tooltip_text=_("Insert Link…"), css_classes=["flat"])
        link.connect("clicked", lambda _b: self._insert_link())
        self.format_tools.append(link)
        image = Gtk.Button(icon_name="insert-image-symbolic", css_classes=["flat"],
                           tooltip_text=_("Insert Image… (or paste one with Ctrl+V)"))
        image.connect("clicked", lambda _b: self._choose_image())
        self.format_tools.append(image)
        format_bar = Gtk.Box(spacing=12)
        format_bar.append(self.format_group)
        format_bar.append(self.format_tools)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          margin_top=6, margin_bottom=12, margin_start=12, margin_end=12)
        content.append(fields)
        content.append(self.attachment_list)
        content.append(format_bar)
        content.append(self.body_stack)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(content)
        self.set_child(toolbar)

        for attachment in attachments:
            self._add_attachment(attachment)

        shortcuts = Gtk.ShortcutController(scope=Gtk.ShortcutScope.MANAGED)
        shortcuts.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("<Control>Return"),
            Gtk.CallbackAction.new(lambda *_a: self._send() or True),
        ))
        self.add_controller(shortcuts)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

        book = getattr(window.get_application(), "contacts", None)
        self._completers = [AddressCompleter(row, book) for row in (self.to_row, self.cc_row, self.bcc_row)] \
            if book is not None else []
        self.connect("closed", lambda _d: [c.destroy() for c in self._completers])
        self.connect("close-attempt", self._on_close_attempt)
        self._dirty = False
        self.set_can_close(True)
        if self._format == "html":
            self._ensure_editor(text_to_html(body))
            self.body_stack.set_visible_child_name("html")
        self.format_tools.set_visible(self._format == "html")
        if to:
            if self._format == "html":
                self.editor.focus_start()
            else:
                self.set_focus(self.text_view)
        else:
            self.set_focus(self.to_row)

    # --- format --------------------------------------------------------

    def _ensure_editor(self, initial_html=""):
        if self.editor is None:
            self.editor = HtmlEditor(initial_html)
            self.editor.connect("changed", self._mark_dirty)
            self.body_stack.add_named(self.editor, "html")
        else:
            self.editor.set_html(initial_html)

    def _plain_text(self):
        buffer = self.text_view.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)

    def _on_format_toggled(self, group, _pspec):
        wanted = group.get_active_name()
        if self._reverting or wanted == self._format:
            return
        if wanted == "html":
            self._ensure_editor(text_to_html(self._plain_text()))
            self._show_format("html")
            self.editor.focus_start()
            return
        # HTML -> text loses formatting: ask first.
        alert = Adw.AlertDialog(heading=_("Switch to Plain Text?"),
                                body=_("Formatting such as bold text, lists and links will be removed."))
        alert.add_response("cancel", _("_Cancel"))
        alert.add_response("switch", _("_Switch"))
        alert.set_default_response("switch")
        alert.set_close_response("cancel")

        def response(_alert, answer):
            if answer != "switch":
                self._reverting = True
                group.set_active_name("html")
                self._reverting = False
                return

            def converted(body_html):
                if body_html is not None:
                    self.text_view.get_buffer().set_text(html_to_plain(body_html).rstrip("\n") + "\n")
                self._show_format("plain")
                self.text_view.grab_focus()

            self.editor.get_html(converted)

        alert.connect("response", response)
        alert.present(self)

    def _show_format(self, name):
        self._format = name
        self.body_stack.set_visible_child_name(name)
        self.format_tools.set_visible(name == "html")

    def _insert_link(self):
        if self.editor is None:
            return
        entry = Adw.EntryRow(title=_("Web Address"), input_purpose=Gtk.InputPurpose.URL,
                             activates_default=True)  # Enter = Insert
        group = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        group.append(entry)
        alert = Adw.AlertDialog(heading=_("Insert Link"), extra_child=group)
        alert.add_response("cancel", _("_Cancel"))
        alert.add_response("insert", _("_Insert"))
        alert.set_response_appearance("insert", Adw.ResponseAppearance.SUGGESTED)
        alert.set_default_response("insert")
        alert.set_close_response("cancel")

        def response(_alert, answer):
            if answer == "insert":
                self._apply_link(entry.get_text())

        alert.connect("response", response)
        alert.present(self)

    def _choose_image(self):
        if self.editor is None:
            return
        images = Gtk.FileFilter(name=_("Images"))
        images.add_mime_type("image/*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(images)

        def finished(dialog, result):
            try:
                files = dialog.open_multiple_finish(result)
            except GLib.Error:
                return  # cancelled
            for index in range(files.get_n_items()):
                self._insert_image_file(files.get_item(index))

        Gtk.FileDialog(title=_("Insert Image"), filters=filters, default_filter=images).open_multiple(
            self._window, None, finished)

    def _insert_image_file(self, file: Gio.File):
        name = file.get_basename() or "image"
        try:
            _ok, data, _etag = file.load_contents(None)
        except GLib.Error as err:
            self._window.toast(_("Could not insert {name}: {error}").format(name=name, error=err.message))
            return
        data = bytes(data)
        content_type, _uncertain = Gio.content_type_guess(name, data)
        mime = Gio.content_type_get_mime_type(content_type) or ""
        if mime not in _WEB_IMAGE_TYPES:
            # BMP, TIFF, HEIC and the like: most mail programs cannot show them, so send a PNG.
            try:
                data = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data)).save_to_png_bytes().get_data()
                mime = "image/png"
            except GLib.Error:
                self._window.toast(_("{name} is not a picture Brev can insert.").format(name=name))
                return
        self.editor.insert_image(data, mime, name)

    def _apply_link(self, url):
        url = url.strip()
        if not url:
            return
        if not re.match(r"(https?|mailto):", url, re.I):
            url = ("mailto:" if "@" in url and "/" not in url else "https://") + url
        script = ("var s = window.getSelection();"
                  "if (s.isCollapsed) document.execCommand('insertHTML', false, %s);"
                  "else document.execCommand('createLink', false, %s);"
                  ) % (json.dumps('<a href="%s">%s</a>' % (html.escape(url, quote=True), html.escape(url))),
                       json.dumps(url))
        self.editor.run_script(script)
        self.editor.web.grab_focus()
        self._mark_dirty()

    # --- state ---------------------------------------------------------

    def _mark_dirty(self, *_args):
        self._dirty = True
        self.set_can_close(False)

    def _on_from_changed(self, row, _pspec):
        self._service = self._services[row.get_selected()]

    def _on_close_attempt(self, _dialog):
        alert = Adw.AlertDialog(heading=_("Discard Message?"),
                                body=_("The message you are writing will be lost."))
        alert.add_response("cancel", _("_Keep Editing"))
        alert.add_response("discard", _("_Discard"))
        alert.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.set_default_response("cancel")
        alert.set_close_response("cancel")
        alert.connect("response", lambda _a, r: r == "discard" and self.force_close())
        alert.present(self)

    # --- attachments ---------------------------------------------------

    def _add_attachment(self, attachment):
        self._attachments.append(attachment)
        row = Adw.ActionRow(title=attachment.filename, use_markup=False,
                            subtitle=GLib.format_size(attachment.size))
        row.add_prefix(Gtk.Image(gicon=Gio.content_type_get_symbolic_icon(attachment.content_type)))
        remove = Gtk.Button(icon_name="window-close-symbolic", tooltip_text=_("Remove"),
                            valign=Gtk.Align.CENTER, css_classes=["flat", "circular"])

        def on_remove(_button):
            self._attachments.remove(attachment)
            self.attachment_list.remove(row)
            self.attachment_list.set_visible(bool(self._attachments))

        remove.connect("clicked", on_remove)
        row.add_suffix(remove)
        self.attachment_list.append(row)
        self.attachment_list.set_visible(True)

    def _attach_file(self, file: Gio.File):
        try:
            ok, data, _etag = file.load_contents(None)
        except GLib.Error as err:
            self._window.toast(_("Could not attach {name}: {error}").format(name=file.get_basename(), error=err.message))
            return
        name = file.get_basename() or "attachment"
        ctype, _uncertain = Gio.content_type_guess(name, data)
        mime = Gio.content_type_get_mime_type(ctype) or "application/octet-stream"
        self._add_attachment(Attachment(name, mime, bytes(data)))
        self._mark_dirty()

    def _choose_files(self):
        def finished(dialog, result):
            try:
                files = dialog.open_multiple_finish(result)
            except GLib.Error:
                return
            for i in range(files.get_n_items()):
                self._attach_file(files.get_item(i))

        Gtk.FileDialog(title=_("Attach Files")).open_multiple(self._window, None, finished)

    def _on_drop(self, _target, value, _x, _y):
        for file in value.get_files():
            self._attach_file(file)
        return True

    # --- sending -------------------------------------------------------

    def _error(self, heading, body):
        alert = Adw.AlertDialog(heading=heading, body=body)
        alert.add_response("ok", _("_OK"))
        alert.present(self)

    def _send(self):
        if not self.send_button.get_sensitive():
            return
        to, bad_to = _parse_recipients(self.to_row.get_text())
        cc, bad_cc = _parse_recipients(self.cc_row.get_text())
        bcc, bad_bcc = _parse_recipients(self.bcc_row.get_text())
        invalid = bad_to + bad_cc + bad_bcc
        if invalid:
            self._error(_("Invalid Address"), _("Check these recipients: {list}").format(list=", ".join(invalid)))
            return
        if not (to or cc or bcc):
            self._error(_("No Recipients"), _("Add at least one recipient."))
            self.to_row.grab_focus()
            return

        self._set_busy(True)
        if self._format == "html" and self.editor is not None:
            def got(body_html):
                if body_html is None:
                    self._set_busy(False)
                    self._error(_("Could Not Send Message"), _("The message text could not be read."))
                    return
                self._deliver(to, cc, bcc, None, body_html)
            self.editor.get_html(got)
        else:
            self._deliver(to, cc, bcc, self._plain_text(), None)

    def _deliver(self, to, cc, bcc, text, body_html):
        account = self._service.account
        domain = account.email.rpartition("@")[2] or None

        message = EmailMessage()
        message["From"] = f"{account.display_name} <{account.email}>" if account.display_name else account.email
        message["To"] = _format_pairs(to) if to else "undisclosed-recipients:;"
        if cc:
            message["Cc"] = _format_pairs(cc)
        if bcc:
            message["Bcc"] = _format_pairs(bcc)
        message["Subject"] = self.subject_row.get_text()
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain=domain)
        if self._in_reply_to:
            message["In-Reply-To"] = self._in_reply_to
        if self._references:
            message["References"] = self._references
        message["User-Agent"] = "Brev"
        if body_html is None:
            message.set_content(text)
        else:
            body_html, images = extract_inline_images(body_html, lambda: make_msgid(domain=domain)[1:-1])
            message.set_content(html_to_plain(body_html))  # for mail programs that only show text
            message.add_alternative(
                '<!DOCTYPE html><html><head><meta charset="utf-8"><style>'
                "blockquote{margin:0 0 0 .8ex;padding-left:1ex;border-left:2px solid #c0bfbc;color:#5e5c64}"
                f"</style></head><body>{body_html}</body></html>", subtype="html")
            html_part = message.get_payload()[-1]
            for cid, mime, data in images:
                maintype, _slash, subtype = mime.partition("/")
                html_part.add_related(data, maintype=maintype, subtype=subtype, cid=f"<{cid}>")
        for attachment in self._attachments:
            maintype, _slash, subtype = attachment.content_type.partition("/")
            if maintype == "message" and subtype == "rfc822":
                maintype, subtype = "application", "octet-stream"
            message.add_attachment(attachment.data, maintype=maintype or "application",
                                   subtype=subtype or "octet-stream", filename=attachment.filename)

        self._service.send(message, done=self._on_sent, error=self._on_send_error)

    def _set_busy(self, busy):
        self.send_button.set_sensitive(not busy)
        self.spinner.set_visible(busy)
        self.get_child().get_content().set_sensitive(not busy)

    def _on_sent(self, delivered):
        if delivered:
            self._window.toast(_("Message sent"))
        else:
            self._window.toast(_("You are offline. The message is in the Outbox and will be sent later."))
        self._window._update_offline_banner()
        self.force_close()

    def _on_send_error(self, err):
        self._set_busy(False)
        self._error(_("Could Not Send Message"), describe_error(err))

