from django.contrib.auth.backends import BaseBackend
from django.contrib.auth.models import User
from firebase_admin import auth
from .models import UserProfile
import logging

# Get an instance of a logger
logger = logging.getLogger(__name__)

class FirebaseBackend(BaseBackend):
    def authenticate(self, request, token=None):
        """
        Verifies a Firebase ID token and returns a Django user.
        """
        if token is None:
            return None

        try:
            decoded_token = auth.verify_id_token(token)
            uid = decoded_token['uid']
            email = decoded_token.get('email')

            if not email:
                logger.error("Firebase token decoded, but email was not present.")
                return None

            user, created = User.objects.get_or_create(
                username=email, 
                defaults={'email': email}
            )

            if created:
                logger.info(f"New user created: {email}")
                UserProfile.objects.create(user=user, rewards=1500)
            
            return user

        except Exception as e:
            # Log the specific exception that occurred
            logger.error(f"Exception during Firebase token verification: {e}")
            return None

    def get_user(self, user_id):
        """
        Required method for a Django auth backend.
        """
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None
