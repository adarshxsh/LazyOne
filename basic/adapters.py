"""
Sign-in policy (docs/Development/SECURITY.md):
- Google is the main way in. Google has already verified the email, so those accounts
  are verified by default and never need a confirmation email.
- Email + password sign-up is only open when the site can actually send verification
  emails (settings.EMAIL_SIGNUP_ENABLED); otherwise new users are sent to Google.
  Existing email accounts can still log in.
"""
from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.conf import settings


class AccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        return settings.EMAIL_SIGNUP_ENABLED


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def is_open_for_signup(self, request, sociallogin):
        # Allauth would otherwise inherit the email setting above and block Google sign-ups too.
        return True
