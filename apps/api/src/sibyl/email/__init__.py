"""Email module for transactional emails via Resend."""

from sibyl.email.client import EmailClient, get_email_client
from sibyl.email.templates import (
    EmailTemplate,
    EmailVerificationEmail,
    InvitationEmail,
    PasswordResetEmail,
    WelcomeEmail,
)

__all__ = [
    "EmailClient",
    "EmailTemplate",
    "EmailVerificationEmail",
    "InvitationEmail",
    "PasswordResetEmail",
    "WelcomeEmail",
    "get_email_client",
]
