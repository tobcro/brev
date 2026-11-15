"""Mail accounts configured in GNOME Online Accounts."""

from __future__ import annotations

from dataclasses import dataclass
from gettext import gettext as _

import gi

gi.require_version("Goa", "1.0")
from gi.repository import GLib, Goa  # noqa: E402


class AccountError(Exception):
    pass


# Providers whose servers file a copy of sent mail on their own.
_SERVER_SAVES_SENT = {"google", "ms_graph", "windows_live", "exchange"}


@dataclass
class ServerConfig:
    host: str
    port: int
    security: str  # "ssl", "starttls" or "none"
    user: str
    accept_invalid_certs: bool = False
    use_auth: bool = True


def _prop(obj, name, default=None):
    try:
        value = obj.get_property(name.replace("_", "-"))
    except TypeError:
        return default
    return default if value is None else value


def _security(use_ssl: bool, use_tls: bool) -> str:
    if use_ssl:
        return "ssl"
    return "starttls" if use_tls else "none"


def _split_host(value: str, default_port: int) -> tuple[str, int]:
    host = value or ""
    if host.count(":") == 1:
        name, port = host.split(":")
        if port.isdigit():
            return name, int(port)
    return host, default_port


def _first_str(result) -> str:
    if isinstance(result, str):
        return result
    for value in result:
        if isinstance(value, str):
            return value
    raise AccountError(_("Online Accounts returned no credentials."))


def xoauth2_bytes(user: str, token: str) -> bytes:
    return f"user={user}\x01auth=Bearer {token}\x01\x01".encode()


class Account:
    def __init__(self, obj: Goa.Object):
        self._object = obj
        account = obj.get_account()
        mail = obj.get_mail()

        self.id = account.props.id
        self.provider = account.props.provider_type
        self.provider_name = account.props.provider_name
        self.identity = account.props.presentation_identity
        self.email = mail.props.email_address or self.identity
        self.display_name = mail.props.name or ""

        imap_security = _security(_prop(mail, "imap_use_ssl", False), _prop(mail, "imap_use_tls", False))
        host, port = _split_host(mail.props.imap_host, 993 if imap_security == "ssl" else 143)
        self.imap = ServerConfig(
            host, port, imap_security,
            _prop(mail, "imap_user_name", "") or self.email,
            _prop(mail, "imap_accept_ssl_errors", False),
        )

        smtp_security = _security(_prop(mail, "smtp_use_ssl", False), _prop(mail, "smtp_use_tls", False))
        default_port = {"ssl": 465, "starttls": 587}.get(smtp_security, 25)
        host, port = _split_host(mail.props.smtp_host, default_port)
        self.smtp = ServerConfig(
            host, port, smtp_security,
            _prop(mail, "smtp_user_name", "") or self.email,
            _prop(mail, "smtp_accept_ssl_errors", False),
            _prop(mail, "smtp_use_auth", True),
        )

        self.saves_sent_copy = self.provider not in _SERVER_SAVES_SENT

    @property
    def title(self) -> str:
        return self.email or self.identity

    @property
    def signature(self) -> tuple:
        """Settings that require reconnecting when they change."""
        return (self.id, self.email, self.display_name, self.imap, self.smtp)

    def credentials(self, service: str) -> tuple[str, str]:
        """Return ("oauth2", token) or ("password", secret) for "imap" / "smtp"."""
        return goa_credentials(self._object, self.title, f"{service}-password")


def goa_credentials(obj, title: str, password_id: str) -> tuple[str, str]:
    """Credentials for a GOA object. Safe to call from worker threads (GDBus is thread-safe)."""
    try:
        obj.get_account().call_ensure_credentials_sync(None)
        oauth2 = obj.get_oauth2_based()
        if oauth2 is not None:
            return "oauth2", _first_str(oauth2.call_get_access_token_sync(None))
        password = obj.get_password_based()
        if password is not None:
            return "password", _first_str(password.call_get_password_sync(password_id, None))
    except GLib.Error as err:
        raise AccountError(
            _("{account} needs attention in Settings → Online Accounts.").format(account=title)
        ) from err
    raise AccountError(_("{account} has no supported sign-in method.").format(account=title))


def load_accounts(client: Goa.Client) -> list[Account]:
    accounts = []
    for obj in client.get_accounts():
        mail = obj.get_mail()
        if mail is None or obj.get_account().props.mail_disabled:
            continue
        if not _prop(mail, "imap_supported", True) or not mail.props.imap_host:
            continue
        accounts.append(Account(obj))
    accounts.sort(key=lambda a: a.title.casefold())
    return accounts
