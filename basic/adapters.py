"""
Sign-in policy (docs/Development/SECURITY.md):
- Google is the main way in. Google has already verified the email, so those accounts
  are verified by default and never need a confirmation email.
- Email + password sign-up is only open when the site can actually send verification
  emails (settings.EMAIL_SIGNUP_ENABLED); otherwise new users are sent to Google.
  Existing email accounts can still log in.
"""
import logging

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.conf import settings

logger = logging.getLogger(__name__)


class AccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        return settings.EMAIL_SIGNUP_ENABLED


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def is_open_for_signup(self, request, sociallogin):
        # Allauth would otherwise inherit the email setting above and block Google sign-ups too.
        return True

    def on_authentication_error(self, request, provider, error=None, exception=None, extra_context=None):
        # Allauth shows users a generic "Third-Party Login Failure"; log the real reason
        # (no tokens or codes) so it shows up in the server / Vercel logs.
        state_missing = bool(extra_context and 'state_id' in extra_context and not extra_context.get('state'))
        logger.warning("Social login failed: provider=%s error=%s exception=%s: %s state_missing=%s host=%s secure=%s",
                       getattr(provider, 'id', provider), error, type(exception).__name__ if exception else None,
                       str(exception)[:300] if exception else '', state_missing, request.get_host(), request.is_secure())
