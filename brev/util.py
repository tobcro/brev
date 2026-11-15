"""Formatting helpers."""

from __future__ import annotations

import imaplib
import re
import smtplib
import socket
import ssl
from gettext import gettext as _

from gi.repository import Gio, GLib

from .accounts import AccountError


def _uses_12h_clock() -> bool:
    source = Gio.SettingsSchemaSource.get_default()
    if source is None or source.lookup("org.gnome.desktop.interface", True) is None:
        return False
    return Gio.Settings.new("org.gnome.desktop.interface").get_string("clock-format") == "12h"


def _time_format() -> str:
    return "%-l:%M %p" if _uses_12h_clock() else "%H:%M"


def format_list_date(timestamp: float) -> str:
    if not timestamp:
        return ""
    date = GLib.DateTime.new_from_unix_local(int(timestamp))
    now = GLib.DateTime.new_now_local()
    if date.get_ymd() == now.get_ymd():
        return date.format(_time_format())
    days = now.difference(date) / GLib.TIME_SPAN_DAY
    if 0 <= days < 6:
        return date.format("%a")
    if date.get_year() == now.get_year():
        return date.format(_("%-d %b"))
    return date.format("%x")


def format_full_date(timestamp: float) -> str:
    if not timestamp:
        return ""
    date = GLib.DateTime.new_from_unix_local(int(timestamp))
    return date.format(_("%a %-d %b %Y, {time}").replace("{time}", _time_format()))


def describe_error(err: Exception) -> str:
    if isinstance(err, AccountError):
        return str(err)
    if isinstance(err, socket.gaierror):
        return _("Could not reach the mail server. Check your internet connection.")
    if isinstance(err, ssl.SSLCertVerificationError):
        return _("The mail server’s security certificate is not trusted.")
    if isinstance(err, (TimeoutError, socket.timeout)):
        return _("The mail server did not respond in time.")
    if isinstance(err, smtplib.SMTPAuthenticationError):
        return _("The mail server rejected the sign-in. Check the account in Settings → Online Accounts.")
    if isinstance(err, smtplib.SMTPRecipientsRefused):
        return _("The server refused these recipients: {list}").format(list=", ".join(err.recipients))
    if isinstance(err, ConnectionError):
        return _("The connection to the mail server was lost.")
    text = str(err)
    text = re.sub(r"b'([^']*)'", r"\1", text)
    text = re.sub(r'b"([^"]*)"', r"\1", text)
    if isinstance(err, imaplib.IMAP4.error) and "AUTHENTICATE" in text.upper():
        return _("The mail server rejected the sign-in. Check the account in Settings → Online Accounts.")
    return text or err.__class__.__name__
