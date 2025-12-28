"""Email templates for transactional emails."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class EmailTemplate(ABC):
    """Base class for email templates."""

    @property
    @abstractmethod
    def subject(self) -> str:
        """Email subject line."""
        ...

    @abstractmethod
    def render_html(self) -> str:
        """Render HTML version of email."""
        ...

    def render_text(self) -> str | None:
        """Render plain text version (optional)."""
        return None

    def _wrap_html(self, content: str) -> str:
        """Wrap content in SilkCircuit-themed HTML template."""
        return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sibyl</title>
</head>
<body style="margin: 0; padding: 0; background-color: #0a0812; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background-color: #0a0812;">
        <tr>
            <td align="center" style="padding: 40px 20px;">
                <table role="presentation" width="600" cellspacing="0" cellpadding="0" style="background-color: #12101a; border-radius: 8px; border: 1px solid #80ffea33;">
                    <tr>
                        <td style="padding: 40px;">
                            <h1 style="margin: 0 0 24px; color: #e135ff; font-size: 24px; font-weight: 600;">Sibyl</h1>
                            {content}
                        </td>
                    </tr>
                    <tr>
                        <td style="padding: 20px 40px; border-top: 1px solid #80ffea22;">
                            <p style="margin: 0; color: #666; font-size: 12px;">
                                This email was sent by Sibyl. If you didn't request this, you can safely ignore it.
                            </p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>"""


@dataclass
class PasswordResetEmail(EmailTemplate):
    """Password reset email template."""

    reset_url: str
    user_name: str | None = None
    expires_in_minutes: int = 60

    @property
    def subject(self) -> str:
        return "Reset your Sibyl password"

    def render_html(self) -> str:
        greeting = f"Hi {self.user_name}," if self.user_name else "Hi,"
        content = f"""
            <p style="margin: 0 0 16px; color: #e0e0e0; font-size: 16px; line-height: 1.5;">
                {greeting}
            </p>
            <p style="margin: 0 0 24px; color: #e0e0e0; font-size: 16px; line-height: 1.5;">
                We received a request to reset your password. Click the button below to choose a new password:
            </p>
            <p style="margin: 0 0 24px;">
                <a href="{self.reset_url}" style="display: inline-block; padding: 12px 24px; background-color: #e135ff; color: #fff; text-decoration: none; border-radius: 6px; font-weight: 500;">
                    Reset Password
                </a>
            </p>
            <p style="margin: 0 0 16px; color: #999; font-size: 14px;">
                This link will expire in {self.expires_in_minutes} minutes.
            </p>
            <p style="margin: 0; color: #999; font-size: 14px;">
                If you didn't request a password reset, you can ignore this email.
            </p>
        """
        return self._wrap_html(content)

    def render_text(self) -> str:
        greeting = f"Hi {self.user_name}," if self.user_name else "Hi,"
        return f"""{greeting}

We received a request to reset your password. Visit the link below to choose a new password:

{self.reset_url}

This link will expire in {self.expires_in_minutes} minutes.

If you didn't request a password reset, you can ignore this email.

- Sibyl"""


@dataclass
class InvitationEmail(EmailTemplate):
    """Team/org invitation email template."""

    invite_url: str
    inviter_name: str
    team_name: str
    user_name: str | None = None

    @property
    def subject(self) -> str:
        return f"You've been invited to join {self.team_name} on Sibyl"

    def render_html(self) -> str:
        greeting = f"Hi {self.user_name}," if self.user_name else "Hi,"
        content = f"""
            <p style="margin: 0 0 16px; color: #e0e0e0; font-size: 16px; line-height: 1.5;">
                {greeting}
            </p>
            <p style="margin: 0 0 24px; color: #e0e0e0; font-size: 16px; line-height: 1.5;">
                <strong style="color: #80ffea;">{self.inviter_name}</strong> has invited you to join
                <strong style="color: #80ffea;">{self.team_name}</strong> on Sibyl.
            </p>
            <p style="margin: 0 0 24px;">
                <a href="{self.invite_url}" style="display: inline-block; padding: 12px 24px; background-color: #e135ff; color: #fff; text-decoration: none; border-radius: 6px; font-weight: 500;">
                    Accept Invitation
                </a>
            </p>
            <p style="margin: 0; color: #999; font-size: 14px;">
                If you weren't expecting this invitation, you can ignore this email.
            </p>
        """
        return self._wrap_html(content)

    def render_text(self) -> str:
        greeting = f"Hi {self.user_name}," if self.user_name else "Hi,"
        return f"""{greeting}

{self.inviter_name} has invited you to join {self.team_name} on Sibyl.

Accept the invitation: {self.invite_url}

If you weren't expecting this invitation, you can ignore this email.

- Sibyl"""


@dataclass
class WelcomeEmail(EmailTemplate):
    """Welcome email for new users."""

    user_name: str | None = None
    login_url: str = "https://sibyl.dev/login"

    @property
    def subject(self) -> str:
        return "Welcome to Sibyl"

    def render_html(self) -> str:
        greeting = f"Hi {self.user_name}," if self.user_name else "Welcome,"
        content = f"""
            <p style="margin: 0 0 16px; color: #e0e0e0; font-size: 16px; line-height: 1.5;">
                {greeting}
            </p>
            <p style="margin: 0 0 24px; color: #e0e0e0; font-size: 16px; line-height: 1.5;">
                Welcome to Sibyl! Your account is ready. Sibyl is your Graph-RAG Knowledge Oracle -
                helping you capture, search, and leverage development wisdom across your projects.
            </p>
            <p style="margin: 0 0 24px;">
                <a href="{self.login_url}" style="display: inline-block; padding: 12px 24px; background-color: #e135ff; color: #fff; text-decoration: none; border-radius: 6px; font-weight: 500;">
                    Get Started
                </a>
            </p>
            <p style="margin: 0; color: #999; font-size: 14px;">
                Happy exploring!
            </p>
        """
        return self._wrap_html(content)

    def render_text(self) -> str:
        greeting = f"Hi {self.user_name}," if self.user_name else "Welcome,"
        return f"""{greeting}

Welcome to Sibyl! Your account is ready.

Sibyl is your Graph-RAG Knowledge Oracle - helping you capture, search, and leverage development wisdom across your projects.

Get started: {self.login_url}

Happy exploring!

- Sibyl"""


@dataclass
class EmailVerificationEmail(EmailTemplate):
    """Email verification template."""

    verify_url: str
    user_name: str | None = None

    @property
    def subject(self) -> str:
        return "Verify your Sibyl email"

    def render_html(self) -> str:
        greeting = f"Hi {self.user_name}," if self.user_name else "Hi,"
        content = f"""
            <p style="margin: 0 0 16px; color: #e0e0e0; font-size: 16px; line-height: 1.5;">
                {greeting}
            </p>
            <p style="margin: 0 0 24px; color: #e0e0e0; font-size: 16px; line-height: 1.5;">
                Please verify your email address by clicking the button below:
            </p>
            <p style="margin: 0 0 24px;">
                <a href="{self.verify_url}" style="display: inline-block; padding: 12px 24px; background-color: #e135ff; color: #fff; text-decoration: none; border-radius: 6px; font-weight: 500;">
                    Verify Email
                </a>
            </p>
            <p style="margin: 0; color: #999; font-size: 14px;">
                If you didn't create a Sibyl account, you can ignore this email.
            </p>
        """
        return self._wrap_html(content)

    def render_text(self) -> str:
        greeting = f"Hi {self.user_name}," if self.user_name else "Hi,"
        return f"""{greeting}

Please verify your email address by visiting this link:

{self.verify_url}

If you didn't create a Sibyl account, you can ignore this email.

- Sibyl"""
