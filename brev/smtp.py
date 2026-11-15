"""Sending mail over SMTP with credentials from Online Accounts."""

from __future__ import annotations

import base64
import smtplib
import ssl

from .accounts import xoauth2_bytes


def send_message(account, message):
    cfg = account.smtp
    context = ssl.create_default_context()
    if cfg.accept_invalid_certs:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    if cfg.security == "ssl":
        client = smtplib.SMTP_SSL(cfg.host, cfg.port, context=context, timeout=60)
    else:
        client = smtplib.SMTP(cfg.host, cfg.port, timeout=60)
        client.ehlo()
        if cfg.security == "starttls":
            client.starttls(context=context)
            client.ehlo()

    with client:
        if cfg.use_auth:
            kind, secret = account.credentials("smtp")
            if kind == "oauth2":
                client.ehlo_or_helo_if_needed()
                token = base64.b64encode(xoauth2_bytes(cfg.user, secret)).decode()
                code, response = client.docmd("AUTH", "XOAUTH2 " + token)
                if code == 334:  # error details follow; acknowledge them
                    code, response = client.docmd("")
                if code != 235:
                    raise smtplib.SMTPAuthenticationError(code, response)
            else:
                client.login(cfg.user, secret)
        client.send_message(message)
