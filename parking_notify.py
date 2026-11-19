"""Send a single renewal notification for each installed session generation."""

import os
import smtplib
import ssl
from email.message import EmailMessage
from hashlib import sha256
from urllib.parse import urlsplit

from parking_booking import write_json
from parking_schedule import ZONE


def renewal_origin():
    url = urlsplit(os.environ.get("BOOKKER_RENEWAL_URL", ""))
    if (url.scheme != "https" or not url.hostname or not url.hostname.endswith(".ts.net")
            or url.username or url.password or url.path not in ("", "/") or url.query or url.fragment):
        raise ValueError("Set BOOKKER_RENEWAL_URL to the private Tailscale HTTPS origin")
    port = url.port
    authority = f"{url.hostname}:{port}" if port not in (None, 443) else url.hostname
    return f"https://{authority}"


class RenewalMail:
    def __init__(self):
        self.host = os.environ["BOOKKER_SMTP_HOST"]
        self.port = int(os.environ.get("BOOKKER_SMTP_PORT", "465"))
        self.sender = os.environ["BOOKKER_SMTP_FROM"]
        self.recipient = os.environ["BOOKKER_SMTP_TO"]
        self.login = os.environ.get("BOOKKER_SMTP_USER")
        self.password = os.environ.get("BOOKKER_SMTP_PASSWORD")
        if not all((self.host, self.sender, self.recipient)) or bool(self.login) != bool(self.password):
            raise ValueError("Incomplete SMTP settings")
        self.link = renewal_origin() + "/"

    def message(self, occurrence=None):
        mail = EmailMessage()
        mail["From"] = self.sender
        mail["To"] = self.recipient
        if occurrence:
            release = occurrence.release.astimezone(ZONE)
            close = occurrence.close.astimezone(ZONE)
            mail["Subject"] = f"Bookker sign-in needed for parking on {occurrence.target}"
            window = (f"Bookker parking for {occurrence.target} needs sign-in.\n"
                      f"Release window: {release:%Y-%m-%d %H:%M:%S %Z} through {close:%Y-%m-%d %H:%M:%S %Z}.")
        else:
            mail["Subject"] = "Bookker renewal notification test"
            window = "This tests delivery of the Bookker parking renewal link."
        mail.set_content(f"{window}\n\nConnect your phone to Tailscale, open the private page and tap Start sign-in:\n"
                         f"{self.link}\n\nAfter completing Bookker sign-in and MFA, tap Verify and save.")
        return mail

    def send(self, mail):
        with smtplib.SMTP_SSL(self.host, self.port, timeout=8, context=ssl.create_default_context()) as smtp:
            if self.login:
                smtp.login(self.login, self.password)
            smtp.send_message(mail)


def session_generation(session):
    try:
        stat = session.stat()
    except FileNotFoundError:
        return "missing"
    identity = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns)
    return sha256(repr(identity).encode()).hexdigest()


def alert_expiry(session, generation, occurrence, make_mail):
    if generation != session_generation(session):
        return "session_replaced"
    try:
        mailer = make_mail()
        message = mailer.message(occurrence)
    except (KeyError, ValueError):
        return "unconfigured"
    claim = session.parent / f"expiry-{generation}.json"
    try:
        # Claim before SMTP so concurrent workers and interrupted sends cannot duplicate an alert.
        write_json(claim, {"target_date": occurrence.target.isoformat()}, exclusive=True)
    except FileExistsError:
        return "suppressed"
    try:
        mailer.send(message)
    except Exception:
        return "delivery_unverified"
    return "sent"
