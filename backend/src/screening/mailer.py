"""Outgoing email: SMTP (e.g. Gmail with an app password) or a local outbox of .eml files.

Email bodies are templates in templates/email/ (like prompts, they are data):
`<name>.html` and `<name>.txt`, with {{placeholders}}. HTML values are escaped;
`{{content}}` of the shared _layout.html is the one pre-rendered slot.
"""

from __future__ import annotations

import html
import os
import re
import smtplib
import ssl
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

from .config import AppConfig, ConfigError
from .paths import TEMPLATES_DIR

_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")
_EMAIL = re.compile(r"^[^@\s<>\"',;]+@[^@\s<>\"',;]+\.[A-Za-z]{2,}$")


class EmailError(Exception):
    """The message could not be built or sent."""


def valid_email(address: str | None) -> bool:
    return bool(address and _EMAIL.match(address.strip()))


def mask_email(address: str) -> str:
    """a***@gmail.com — enough for the candidate to recognise, useless to anyone else."""
    local, _, domain = address.partition("@")
    return f"{local[:1]}{'*' * max(2, min(len(local) - 1, 6))}@{domain}"


@dataclass
class InlineImage:
    cid: str          # referenced as <img src="cid:...">
    data: bytes
    subtype: str = "png"
    filename: str = "image.png"


@dataclass
class Email:
    to: str
    subject: str
    text: str
    html: str
    images: list[InlineImage] = field(default_factory=list)
    kind: str = "email"   # for logs / outbox file names: invite, reminder, otp, ...


def _render(template: str, values: dict[str, str], *, escape: bool, raw: frozenset[str] = frozenset()) -> str:
    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in values:
            raise EmailError(f"email template placeholder {{{{{key}}}}} has no value")
        v = str(values[key])
        return html.escape(v) if escape and key not in raw else v
    return _PLACEHOLDER.sub(sub, template)


def render_email(name: str, *, to: str, subject: str, values: dict[str, str],
                 images: list[InlineImage] | None = None) -> Email:
    """templates/email/<name>.{html,txt} -> Email. The HTML is wrapped in _layout.html."""
    try:
        body_html = (TEMPLATES_DIR / "email" / f"{name}.html").read_text(encoding="utf-8")
        body_txt = (TEMPLATES_DIR / "email" / f"{name}.txt").read_text(encoding="utf-8")
        layout = (TEMPLATES_DIR / "email" / "_layout.html").read_text(encoding="utf-8")
    except OSError as e:
        raise EmailError(f"email template missing: {e}") from e
    content = _render(body_html, values, escape=True)
    full_html = _render(layout, {**values, "subject": subject, "content": content}, escape=True,
                        raw=frozenset({"content"}))
    return Email(to=to, subject=subject, text=_render(body_txt, values, escape=False).strip() + "\n",
                 html=full_html, images=images or [], kind=name)


def _message(mail: Email, from_name: str, from_address: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = mail.subject
    msg["From"] = formataddr((from_name, from_address))
    msg["To"] = mail.to
    msg["Message-ID"] = make_msgid(domain=from_address.partition("@")[2] or None)
    msg.set_content(mail.text)
    msg.add_alternative(mail.html, subtype="html")
    if mail.images:
        # Gmail blocks data: URIs, so images go in as inline (cid:) parts of the HTML alternative.
        html_part = msg.get_payload()[1]
        for img in mail.images:
            html_part.add_related(img.data, maintype="image", subtype=img.subtype, cid=f"<{img.cid}>",
                                  filename=img.filename, disposition="inline")
    return msg


class Mailer(ABC):
    mode: str

    @abstractmethod
    def send(self, mail: Email) -> str:
        """Send (or file) the message; returns a short description for logs. Raises EmailError."""


class SmtpMailer(Mailer):
    mode = "smtp"

    def __init__(self, *, host: str, port: int, user: str, password: str, security: str,
                 from_name: str, from_address: str, timeout: int = 30):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.security, self.from_name, self.from_address, self.timeout = security, from_name, from_address, timeout

    def send(self, mail: Email) -> str:
        msg = _message(mail, self.from_name, self.from_address)
        try:
            if self.security == "ssl":
                with smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout,
                                      context=ssl.create_default_context()) as s:
                    s.login(self.user, self.password)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as s:
                    s.starttls(context=ssl.create_default_context())
                    s.login(self.user, self.password)
                    s.send_message(msg)
        except smtplib.SMTPAuthenticationError as e:
            raise EmailError("SMTP login failed. For Gmail, SMTP_PASS must be a 16-character app password "
                             "(Google Account -> Security -> App passwords), not your normal password.") from e
        except (smtplib.SMTPException, OSError) as e:
            raise EmailError(f"could not send email via {self.host}:{self.port}: {type(e).__name__}: {e}") from e
        return f"sent to {mail.to} via {self.host}"


class OutboxMailer(Mailer):
    """Writes each message to <data>/outbox/<time>_<kind>_<id>.eml; open them in any mail app."""
    mode = "outbox"

    def __init__(self, folder: Path, *, from_name: str, from_address: str):
        self.folder, self.from_name, self.from_address = folder, from_name, from_address

    def send(self, mail: Email) -> str:
        self.folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")  # sortable, µs apart
        path = self.folder / f"{stamp}_{mail.kind}_{uuid.uuid4().hex[:8]}.eml"
        path.write_bytes(bytes(_message(mail, self.from_name, self.from_address)))
        return f"written to outbox {path.name} (not sent)"


def make_mailer(config: AppConfig) -> Mailer:
    ec = config.settings.email
    env = lambda k: os.environ.get(k, "").strip()  # noqa: E731
    user = env(ec.smtp_user_env)
    from_address = env(ec.from_address_env) or user or "screening@localhost"
    if ec.mode == "outbox":
        return OutboxMailer(config.data.root / "outbox", from_name=ec.from_name, from_address=from_address)
    password = env(ec.smtp_pass_env)
    if not user or not password:
        raise ConfigError(f"email.mode is smtp but {ec.smtp_user_env} / {ec.smtp_pass_env} are not set in .env. "
                          "For Gmail use your address and a 16-character app password; "
                          "or set EMAIL_MODE=outbox to write emails to files instead.")
    security = (env(ec.smtp_security_env) or "starttls").lower()
    if security not in ("starttls", "ssl"):
        raise ConfigError(f"{ec.smtp_security_env} must be starttls or ssl (got {security!r})")
    port_raw = env(ec.smtp_port_env) or ("465" if security == "ssl" else "587")
    try:
        port = int(port_raw)
    except ValueError:
        raise ConfigError(f"{ec.smtp_port_env} must be a number (got {port_raw!r})") from None
    return SmtpMailer(host=env(ec.smtp_host_env) or "smtp.gmail.com", port=port, user=user, password=password,
                      security=security, from_name=ec.from_name, from_address=from_address)
