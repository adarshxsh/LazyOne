import logging

from django.contrib.auth.backends import BaseBackend
from django.contrib.auth.models import User
from firebase_admin import auth

from .services.identity import user_from_firebase_claims

logger = logging.getLogger(__name__)


class FirebaseBackend(BaseBackend):
    """Session login with a Firebase ID token. Users are keyed by firebase_uid, not email."""

    def authenticate(self, request, token=None):
        if token is None:
            return None
        try:
            claims = auth.verify_id_token(token)
        except Exception as e:  # noqa: BLE001 - any verification failure means "not authenticated"
            logger.warning("Firebase token verification failed: %s", e)
            return None
        return user_from_firebase_claims(claims)

    def get_user(self, user_id):
        return User.objects.filter(pk=user_id).first()
