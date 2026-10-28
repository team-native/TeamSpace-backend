import logging
import smtplib
import ssl
from email.message import EmailMessage

from app.core.config import settings

logger = logging.getLogger(__name__)


class EmailConfigurationError(RuntimeError):
    """Raised when SMTP settings are missing or inconsistent."""


class EmailDeliveryError(RuntimeError):
    """Raised when the SMTP server rejects or cannot deliver a message."""


def send_email(
    to_email: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
) -> None:
    """Send one email using SMTP settings loaded from the project .env file."""
    required_settings = {
        "SMTP_HOST": settings.smtp_host,
        "SMTP_FROM_EMAIL": settings.smtp_from_email,
    }
    missing = [name for name, value in required_settings.items() if not value]
    if missing:
        raise EmailConfigurationError(f"Missing SMTP settings: {', '.join(missing)}")
    if settings.smtp_use_ssl and settings.smtp_use_tls:
        raise EmailConfigurationError(
            "Set only one of SMTP_USE_SSL and SMTP_USE_TLS to true."
        )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from_email or ""
    message["To"] = to_email
    message.set_content(text_body)
    if html_body:
        message.add_alternative(html_body, subtype="html")

    try:
        context = ssl.create_default_context()
        if settings.smtp_use_ssl:
            with smtplib.SMTP_SSL(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.smtp_timeout_seconds,
                context=context,
            ) as server:
                _authenticate(server)
                server.send_message(message)
        else:
            with smtplib.SMTP(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.smtp_timeout_seconds,
            ) as server:
                server.ehlo()
                if settings.smtp_use_tls:
                    server.starttls(context=context)
                    server.ehlo()
                _authenticate(server)
                server.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        logger.exception("SMTP delivery failed")
        raise EmailDeliveryError("SMTP server could not deliver the email.") from exc


def _authenticate(server: smtplib.SMTP | smtplib.SMTP_SSL) -> None:
    if settings.smtp_username:
        server.login(settings.smtp_username, settings.smtp_password or "")
