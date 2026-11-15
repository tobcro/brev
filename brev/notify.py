"""Desktop notifications for new mail.

They deliberately say only how many emails arrived: no subjects or senders, so
nothing private shows up in banners, the notification list or on the lock screen.
"""

from __future__ import annotations

import logging
import os
from gettext import gettext as _, ngettext

from gi.repository import Gio, GLib

from . import APP_ID

log = logging.getLogger(__name__)

_ID = "new-mail"


def notification_text(count: int) -> tuple[str, str]:
    title = ngettext("New Email", "New Emails", count)
    body = (_("A new email has arrived.") if count == 1
            else ngettext("{n} new email has arrived.", "{n} new emails have arrived.", count).format(n=count))
    return title, body


class MailNotifier:
    """Uses GApplication notifications when Brev is installed (GNOME needs the .desktop
    file to show them), and the freedesktop notification service when run from source."""

    def __init__(self, application, icon_path: str | None = None):
        self._app = application
        self._icon_path = icon_path
        self._installed = _desktop_file_installed()
        self._proxy = None
        self._last_id = 0
        log.debug("Notifications via %s", "GNotification" if self._installed else "org.freedesktop.Notifications")

    def new_mail(self, count: int):
        if count <= 0:
            return
        title, body = notification_text(count)
        if self._installed:
            notification = Gio.Notification.new(title)
            notification.set_body(body)
            notification.set_priority(Gio.NotificationPriority.NORMAL)
            if hasattr(notification, "set_category"):
                notification.set_category("email.arrived")
            self._app.send_notification(_ID, notification)  # same id: replaces the previous one
        else:
            self._notify_freedesktop(title, body)

    def withdraw(self):
        if self._installed:
            self._app.withdraw_notification(_ID)

    # --- fallback for running from the source tree ---------------------

    def _notify_freedesktop(self, title, body):
        try:
            if self._proxy is None:
                self._proxy = Gio.DBusProxy.new_for_bus_sync(
                    Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, None,
                    "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                    "org.freedesktop.Notifications", None)
                self._proxy.connect("g-signal", self._on_signal)
            hints = {
                "desktop-entry": GLib.Variant("s", APP_ID),
                "category": GLib.Variant("s", "email.arrived"),
            }
            result = self._proxy.call_sync(
                "Notify",
                GLib.Variant("(susssasa{sv}i)", ("Brev", self._last_id, self._icon_path or "mail-unread",
                                                 title, body, ["default", _("Open")], hints, -1)),
                Gio.DBusCallFlags.NONE, 5000, None)
            self._last_id = result.unpack()[0]
        except GLib.Error as err:
            log.warning("Could not show a notification: %s", err.message)

    def _on_signal(self, _proxy, _sender, signal, parameters):
        if signal == "ActionInvoked" and parameters.unpack()[0] == self._last_id:
            self._app.activate()


def _desktop_file_installed() -> bool:
    try:
        return Gio.DesktopAppInfo.new(APP_ID + ".desktop") is not None
    except TypeError:
        return False


def default_icon_path() -> str | None:
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), "data", "icons", "hicolor", "scalable", "apps", APP_ID + ".svg")
    return path if os.path.exists(path) else None
