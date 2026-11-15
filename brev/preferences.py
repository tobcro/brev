"""The Preferences dialog."""

from __future__ import annotations

from gettext import gettext as _, ngettext

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GObject, Gtk  # noqa: E402

from .html_view import AVAILABLE as HTML_AVAILABLE  # noqa: E402

_FORMATS = ("plain", "html")
_INTERVALS = (0, 15, 30, 60, 300, 900)  # seconds; 0 = off


def _interval_label(seconds: int) -> str:
    if seconds == 0:
        return _("Off")
    if seconds < 60:
        return ngettext("{n} second", "{n} seconds", seconds).format(n=seconds)
    minutes = seconds // 60
    return ngettext("{n} minute", "{n} minutes", minutes).format(n=minutes)


def _signature_editor(settings, key):
    """A small multi-line editor bound to a settings key."""
    view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False,
                        top_margin=10, bottom_margin=10, left_margin=12, right_margin=12,
                        css_classes=["signature-editor"])
    view.update_property([Gtk.AccessibleProperty.LABEL], [key])
    buffer = view.get_buffer()
    buffer.set_text(settings.get_property(key))
    buffer.connect("changed", lambda b: settings.set_property(
        key, b.get_text(b.get_start_iter(), b.get_end_iter(), False)))
    scroller = Gtk.ScrolledWindow(child=view, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                  min_content_height=96, max_content_height=200,
                                  propagate_natural_height=True)
    return Gtk.Frame(child=scroller), view


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, settings):
        super().__init__(title=_("Preferences"), search_enabled=False)
        page = Adw.PreferencesPage(title=_("General"), icon_name="preferences-system-symbolic")
        self.add(page)

        # --- Reading
        reading = Adw.PreferencesGroup(title=_("Reading"))
        images = Adw.SwitchRow(
            title=_("Always Show Images"),
            subtitle=_("Load pictures from the internet in every email. "
                       "Senders can then tell when you open their messages."))
        settings.bind_property("always-show-images", images, "active",
                               GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        reading.add(images)
        page.add(reading)

        # --- New mail and offline
        mail = Adw.PreferencesGroup(title=_("New Mail and Offline"))
        intervals = list(_INTERVALS)
        if settings.refresh_interval not in intervals:  # a custom value from the settings file
            intervals.append(settings.refresh_interval)
            intervals.sort()
        self._intervals = intervals
        self.refresh_row = Adw.ComboRow(
            title=_("Check for New Mail"),
            subtitle=_("Off: only when you press Refresh."),
            model=Gtk.StringList.new([_interval_label(i) for i in intervals]))
        self.refresh_row.set_selected(intervals.index(settings.refresh_interval))
        self.refresh_row.connect("notify::selected", lambda row, _p: settings.set_property(
            "refresh-interval", self._intervals[row.get_selected()]))
        mail.add(self.refresh_row)
        self.cache_row = Adw.SpinRow.new_with_range(1, 365, 1)
        self.cache_row.set_title(_("Keep Mail for Offline Use"))
        self.cache_row.set_subtitle(_("Days of mail saved on this computer. Older mail is removed when Brev quits."))
        self.cache_row.set_value(settings.cache_days)
        self.cache_row.connect("notify::value", lambda row, _p: settings.set_property(
            "cache-days", int(row.get_value())))
        mail.add(self.cache_row)
        page.add(mail)

        # --- Writing
        writing = Adw.PreferencesGroup(title=_("Writing"))
        formats = Gtk.StringList.new([_("Plain Text"), _("HTML")])
        self.format_row = Adw.ComboRow(
            title=_("Format for New Messages"),
            subtitle=_("You can still switch for each message while writing it.") if HTML_AVAILABLE
            else _("HTML needs WebKitGTK 6 (gir1.2-webkit-6.0)."),
            model=formats, sensitive=HTML_AVAILABLE)
        self.format_row.set_selected(_FORMATS.index(settings.compose_format) if HTML_AVAILABLE else 0)
        self.format_row.connect("notify::selected",
                                lambda row, _p: settings.set_property("compose-format", _FORMATS[row.get_selected()]))
        writing.add(self.format_row)
        page.add(writing)

        # --- Signatures
        for key, title, description in (
            ("signature_new", _("Signature for New Messages"),
             _("Added below the text of messages you start yourself.")),
            ("signature_reply", _("Signature for Replies and Forwards"),
             _("Added below your text, above the quoted message.")),
        ):
            group = Adw.PreferencesGroup(title=title, description=description)
            frame, view = _signature_editor(settings, key)
            group.add(frame)
            page.add(group)
            setattr(self, key + "_view", view)
