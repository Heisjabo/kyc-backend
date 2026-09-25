"""Where email will be sent from.

Nothing is sent yet. Choosing a provider, owning a sending domain, and passing
its SPF and DKIM checks is a piece of work of its own, and a password reset that
silently fails to arrive is worse than one that was never offered.

Until then this records what would have been sent. The flow around it is real:
the reset token is generated, hashed, stored with an expiry and consumed once,
exactly as it will be when the mail goes out. Turning sending on means filling in
one function.

In development the token is handed back in the response so the flow can be tried
end to end. `check_configuration` refuses to start with that behaviour anywhere
but a developer's machine.
"""

import logging

from app.core.config import settings

logger = logging.getLogger(__name__)


def send_password_reset(email: str, token: str) -> bool:
    """Send someone a link for setting a new password.

    Returns whether it was actually sent, which is currently always False.
    """
    link = f"{settings.DASHBOARD_URL.rstrip('/')}/reset-password?token={token}"
    if not settings.EMAIL_ENABLED:
        # The token itself is never logged: a log with a working reset link in it
        # is a way into an account.
        logger.info("Password reset requested for %s. Email is off, so nothing was sent.", email)
        return False

    # TODO: hand `link` to the provider once there is one.
    logger.warning("EMAIL_ENABLED is set but no provider is wired up yet; %s got nothing", email)
    return False


def reveal_token_to_caller() -> bool:
    """Whether the reset token may be returned in the API response.

    True only on a developer's machine, and only while there is no email. It is
    how the reset flow is tested before a provider exists; anywhere else it would
    be a way to take over any account by asking politely.
    """
    return settings.ENVIRONMENT == "development" and not settings.EMAIL_ENABLED
