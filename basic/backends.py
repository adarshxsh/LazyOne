from django.contrib.auth.backends import BaseBackend
from django.contrib.auth.models import User
from firebase_admin import auth
from .models import UserProfile
import logging
from .firebase_init import initialize_firebase # Import the new robust initializer

# Get an instance of a logger
logger = logging.getLogger(__name__)

class FirebaseBackend(BaseBackend):
    def authenticate(self, request, token=None):
        """
        Verifies a Firebase ID token and returns a Django user.
        """
        initialize_firebase() # Ensure Firebase is initialized before any auth call

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

            # Ensure profile exists and firebase_uid is set
            profile, profile_created = UserProfile.objects.get_or_create(user=user)
            if profile_created:
                logger.info(f"New user profile created for: {email}")
                profile.rewards = 1500 # Set initial rewards for new profiles
            
            if not profile.firebase_uid:
                profile.firebase_uid = uid
                profile.save()
            
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
