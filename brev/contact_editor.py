"""Add, edit or delete one contact."""

from __future__ import annotations

import copy
from gettext import gettext as _

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from .contacts import Contact, ContactConflict  # noqa: E402
from .util import describe_error  # noqa: E402


class _ListGroup(Adw.PreferencesGroup):
    """A group of entry rows (email addresses or phone numbers) with add and remove buttons."""

    def __init__(self, title, row_title, add_label, values, input_purpose, on_change):
        super().__init__(title=title)
        self._row_title = row_title
        self._purpose = input_purpose
        self._on_change = on_change
        self._rows: list[Adw.EntryRow] = []
        self._add = Adw.ButtonRow(title=add_label, start_icon_name="list-add-symbolic")
        self._add.connect("activated", lambda _r: self.add_row("", focus=True))
        for value in values:
            self.add_row(value)
        self.add(self._add)

    def add_row(self, value, focus=False):
        row = Adw.EntryRow(title=self._row_title, text=value, input_purpose=self._purpose)
        remove = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text=_("Remove"),
                            valign=Gtk.Align.CENTER, css_classes=["flat"])
        remove.connect("clicked", lambda _b: self._remove(row))
        row.add_suffix(remove)
        row.connect("changed", lambda _r: self._on_change())
        self._rows.append(row)
        # Keep the "add" button last.
        if self._add.get_parent() is not None:
            self.remove(self._add)
            self.add(row)
            self.add(self._add)
        else:
            self.add(row)
        if focus:
            row.grab_focus()
        self._on_change()

    def _remove(self, row):
        self._rows.remove(row)
        self.remove(row)
        self._on_change()

    def values(self) -> list[str]:
        return [r.get_text().strip() for r in self._rows if r.get_text().strip()]


class ContactEditor(Adw.Dialog):
    """contact=None makes a new contact."""

    def __init__(self, window, book, contact: Contact | None, online: bool, prefill_email: str = "",
                 prefill_name: str = ""):
        super().__init__(title=_("New Contact") if contact is None else _("Edit Contact"),
                         content_width=460, content_height=620)
        self._window = window
        self._book = book
        self._contact = contact
        self._sources = book.writable_sources()
        self._online = online

        header = Adw.HeaderBar()
        self.save_button = Gtk.Button(label=_("_Save") if contact else _("_Add"), use_underline=True,
                                      css_classes=["suggested-action"])
        self.save_button.connect("clicked", lambda _b: self._save())
        header.pack_end(self.save_button)
        self.spinner = Adw.Spinner(visible=False)
        header.pack_end(self.spinner)

        page = Adw.PreferencesPage()
        self.account_row = None
        if contact is None and len(self._sources) > 1:
            names = Gtk.StringList.new([s.title for s in self._sources])
            self.account_row = Adw.ComboRow(title=_("Account"), model=names)
            group = Adw.PreferencesGroup()
            group.add(self.account_row)
            page.add(group)

        details = Adw.PreferencesGroup()
        self.name_row = Adw.EntryRow(title=_("Name"), text=contact.name if contact else prefill_name)
        self.org_row = Adw.EntryRow(title=_("Organization"), text=contact.org if contact else "")
        for row in (self.name_row, self.org_row):
            row.connect("changed", lambda _r: self._update_state())
            details.add(row)
        page.add(details)

        emails = list(contact.emails) if contact else ([prefill_email] if prefill_email else [""])
        self.emails = _ListGroup(_("Email Addresses"), _("Email"), _("Add Email Address"), emails,
                                 Gtk.InputPurpose.EMAIL, self._update_state)
        page.add(self.emails)
        phones = list(contact.phones) if contact else []
        self.phones = _ListGroup(_("Phone Numbers"), _("Phone"), _("Add Phone Number"), phones,
                                 Gtk.InputPurpose.PHONE, self._update_state)
        page.add(self.phones)

        if contact is not None:
            danger = Adw.PreferencesGroup()
            # A plain button rather than a row: the same size and type as Save, filled red.
            delete = Gtk.Button(label=_("_Delete Contact"), use_underline=True, hexpand=True,
                                css_classes=["destructive-action", "delete-contact"])
            delete.connect("clicked", lambda _b: self._confirm_delete())
            danger.add(delete)
            page.add(danger)
            source = book.sources.get(contact.source)
            if source is not None:
                details.set_description(_("Saved in {account}. Fields Brev doesn’t show, such as photos "
                                          "and birthdays, are kept.").format(account=source.title))

        self.banner = Adw.Banner(title=_("You are offline. Contacts can be changed when you are back online."),
                                 revealed=not online)
        toolbar = Adw.ToolbarView(content=page)
        toolbar.add_top_bar(header)
        toolbar.add_top_bar(self.banner)
        self.set_child(toolbar)
        self.set_focus(self.name_row)
        self._update_state()

    # --- state ---------------------------------------------------------------

    def _edited(self) -> Contact:
        contact = copy.deepcopy(self._contact) if self._contact else Contact(uid="")
        contact.name = self.name_row.get_text().strip()
        contact.org = self.org_row.get_text().strip()
        contact.emails = self.emails.values()
        contact.phones = self.phones.values()
        return contact

    def _update_state(self):
        if not hasattr(self, "phones") or not hasattr(self, "save_button"):
            return  # still being built
        edited = self._edited()
        has_content = bool(edited.name or edited.org or edited.emails or edited.phones)
        changed = self._contact is None or (edited.name, edited.org, edited.emails, edited.phones) != (
            self._contact.name, self._contact.org, self._contact.emails, self._contact.phones)
        self.save_button.set_sensitive(self._online and has_content and changed and not self._book.busy)

    def _set_busy(self, busy):
        self.spinner.set_visible(busy)
        self.get_child().get_content().set_sensitive(not busy)
        self.save_button.set_sensitive(not busy)
        if not busy:
            self._update_state()

    def _alert(self, heading, body):
        alert = Adw.AlertDialog(heading=heading, body=body)
        alert.add_response("ok", _("_OK"))
        alert.present(self)

    # --- actions -------------------------------------------------------------

    def _save(self):
        edited = self._edited()
        bad = [e for e in edited.emails if "@" not in e or " " in e]
        if bad:
            self._alert(_("Invalid Email Address"), _("Check: {list}").format(list=", ".join(bad)))
            return
        self._set_busy(True)
        if self._contact is None:
            source = self._sources[self.account_row.get_selected() if self.account_row else 0]
            edited.source = source.id
            self._book.add(source.id, edited, self._done(_("Contact added")), self._failed)
        else:
            self._book.save(edited, self._done(_("Contact saved")), self._failed)

    def _confirm_delete(self):
        source = self._book.sources.get(self._contact.source)
        alert = Adw.AlertDialog(
            heading=_("Delete {name}?").format(name=self._contact.label),
            body=_("The contact is also removed from {account} and your other devices.").format(
                account=source.title if source else _("the account")))
        alert.add_response("cancel", _("_Cancel"))
        alert.add_response("delete", _("_Delete"))
        alert.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.set_default_response("cancel")
        alert.set_close_response("cancel")

        def response(_alert, answer):
            if answer == "delete":
                self._set_busy(True)
                self._book.remove(self._contact, self._done(_("Contact deleted")), self._failed)

        alert.connect("response", response)
        alert.present(self)

    def _done(self, message):
        def done():
            self._window.toast(message)
            self.force_close()
        return done

    def _failed(self, err):
        self._set_busy(False)
        if isinstance(err, ContactConflict):
            self._alert(_("Contact Changed Elsewhere"),
                        _("This contact was changed on another device after Brev loaded it. Brev is "
                          "loading the latest version; open it again to make your change."))
            self.force_close()
            return
        self._alert(_("Could Not Save Contact"), describe_error(err))
