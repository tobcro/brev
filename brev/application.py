"""Application setup: actions, shortcuts, styling, about."""

from __future__ import annotations

import logging
import os
from gettext import gettext as _

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, HOMEPAGE, VERSION  # noqa: E402
from .cache import Cache, default_path  # noqa: E402
from .contacts import ContactBook  # noqa: E402
from .notify import MailNotifier, default_icon_path  # noqa: E402
from .settings import Settings  # noqa: E402
from .window import BrevWindow  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))

SHORTCUTS = [
    (_("General"), [
        (_("New Message"), "win.compose", ["<Control>n"]),
        (_("Search"), "win.search", ["<Control>f"]),
        (_("Refresh"), "win.refresh", ["F5"]),
        (_("Show Only Unread Messages"), "win.unread-only", ["<Control>u"]),
        (_("Contacts"), "win.contacts", ["<Control><Shift>c"]),
        (_("Preferences"), "app.preferences", ["<Control>comma"]),
        (_("Keyboard Shortcuts"), "app.shortcuts", ["<Control>question"]),
        (_("Quit"), "app.quit", ["<Control>q"]),
    ]),
    (_("Messages"), [
        (_("Reply"), "win.reply", ["<Control>r"]),
        (_("Reply to All"), "win.reply-all", ["<Control><Shift>r"]),
        (_("Forward"), "win.forward", ["<Control>l"]),
        (_("Archive"), "win.archive", ["<Control>e"]),
        (_("Move to Trash"), "win.trash", ["Delete"]),
        (_("Mark as Read/Unread"), "win.toggle-read", ["<Control>i"]),
        (_("Star/Unstar"), "win.toggle-star", ["<Control>d"]),
    ]),
    (_("Compose"), [
        (_("Send"), None, ["<Control>Return"]),
    ]),
]


class BrevApplication(Adw.Application):
    def __init__(self, demo=False):
        flags = Gio.ApplicationFlags.DEFAULT_FLAGS
        if demo:
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=APP_ID, flags=flags)
        self.demo = demo
        self._cache = None
        self._contacts = None
        GLib.set_application_name("Brev")

    def do_startup(self):
        Adw.Application.do_startup(self)
        Gtk.Window.set_default_icon_name(APP_ID)
        self.notifier = MailNotifier(self, default_icon_path())
        self.settings = Settings(Settings.default_path(self.demo))

        display = Gdk.Display.get_default()
        icons = os.path.join(os.path.dirname(_HERE), "data", "icons")
        if os.path.isdir(icons):
            Gtk.IconTheme.get_for_display(display).add_search_path(icons)
        css = Gtk.CssProvider()
        css.load_from_path(os.path.join(_HERE, "style.css"))
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        for name, callback in (
            ("quit", lambda *_a: self.quit()),
            ("about", lambda *_a: self._show_about()),
            ("preferences", lambda *_a: self._show_preferences()),
            ("shortcuts", lambda *_a: self._show_shortcuts()),
            ("accounts", lambda *_a: self._open_online_accounts()),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)

        for _section, items in SHORTCUTS:
            for _title, action, accels in items:
                if action:
                    self.set_accels_for_action(action, accels)

    def do_activate(self):
        window = self.props.active_window or BrevWindow(self)
        window.present()

    @property
    def cache(self):
        if self._cache is None:
            self._cache = Cache(default_path(self.demo))
            # Also purge on start, in case the last session did not exit cleanly.
            self._cache.purge()
        return self._cache

    @property
    def contacts(self) -> ContactBook:
        if self._contacts is None:
            self._contacts = ContactBook(self.cache)
        return self._contacts

    def do_shutdown(self):
        """Purge the cache down to the last KEEP_DAYS days of mail."""
        if self._cache is not None:
            accounts = None
            for window in self.get_windows():
                if isinstance(window, BrevWindow):
                    for service in window.services:
                        service.shutdown()
                    if window.goa_client is not None or self.demo:
                        accounts = [s.account.id for s in window.services]
            try:
                self._cache.purge(accounts=accounts)
            except Exception:
                logging.getLogger(__name__).warning("Could not purge the cache", exc_info=True)
            self._cache.close()
            self._cache = None
        Adw.Application.do_shutdown(self)

    def _show_about(self):
        about = Adw.AboutDialog(
            application_name="Brev",
            application_icon=APP_ID,
            developer_name="Tobias",
            version=VERSION,
            comments=_("Email for GNOME, using the accounts you already have in Settings → Online "
                       "Accounts. No setup wizard, no trackers, no nonsense."),
            website=HOMEPAGE,
            issue_url=HOMEPAGE + "/issues",
            license_type=Gtk.License.GPL_3_0,
            copyright="© 2026 Tobias",
        )
        about.add_credit_section(_("Built with AI"), ["Claude Code https://claude.com/claude-code"])
        about.present(self.props.active_window)

    def _show_preferences(self):
        from .preferences import PreferencesDialog
        PreferencesDialog(self.settings).present(self.props.active_window)

    def _show_shortcuts(self):
        dialog = Adw.ShortcutsDialog()
        for title, items in SHORTCUTS:
            section = Adw.ShortcutsSection(title=title)
            for label, _action, accels in items:
                section.add(Adw.ShortcutsItem(title=label, accelerator=" ".join(accels)))
            dialog.add(section)
        dialog.present(self.props.active_window)

    def _open_online_accounts(self):
        try:
            Gio.Subprocess.new(["gnome-control-center", "online-accounts"], Gio.SubprocessFlags.NONE)
        except GLib.Error as err:
            window = self.props.active_window
            if isinstance(window, BrevWindow):
                window.toast(_("Could not open Settings: {error}").format(error=err.message))
