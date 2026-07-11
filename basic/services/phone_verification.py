import logging
from firebase_admin import auth
from firebase_admin.auth import InvalidIdTokenError, ExpiredIdTokenError
from basic.models import UserProfile

logger = logging.getLogger(__name__)

class PhoneVerificationService:
    """
    Service responsible for verifying Firebase phone authentication tokens
    and syncing the verified state to the Django UserProfile database.
    """

    @staticmethod
    def verify_and_extract_phone(token: str) -> dict:
        """
        Cryptographically verifies the Firebase ID token and extracts the phone number.
        Returns a dictionary with status and details.
        
        State Machine Transitions:
        UNVERIFIED -> VERIFIED (if token is valid and contains phone_number)
        VERIFICATION_PENDING -> UNVERIFIED (if token is invalid)
        """
        if not token:
            return {"status": "error", "message": "Token is required.", "phone_number": None}

        try:
            # Verify the JWT using Firebase Admin SDK
            decoded_token = auth.verify_id_token(token)
            
            # Extract the phone number from the payload
            phone_number = decoded_token.get('phone_number')

            if not phone_number:
                logger.warning("Firebase token decoded, but no phone_number found.")
                return {"status": "error", "message": "Token does not contain a phone number.", "phone_number": None}

            logger.info(f"Phone number successfully verified: {phone_number}")
            return {"status": "success", "message": "Token verified successfully.", "phone_number": phone_number}

        except ExpiredIdTokenError:
            logger.warning("Firebase token has expired.")
            return {"status": "error", "message": "Token has expired.", "phone_number": None}
        except InvalidIdTokenError:
            logger.warning("Firebase token is invalid.")
            return {"status": "error", "message": "Invalid token.", "phone_number": None}
        except Exception as e:
            logger.error(f"Unexpected error during Firebase token verification: {str(e)}")
            return {"status": "error", "message": "An unexpected error occurred during verification.", "phone_number": None}

    @staticmethod
    def mark_user_phone_verified(user, phone_number: str) -> bool:
        """
        Updates the Django UserProfile to reflect the verified state.
        """
        try:
            profile = UserProfile.objects.get(user=user)
            profile.phone_number = phone_number
            profile.is_phone_verified = True
            profile.save()
            return True
        except UserProfile.DoesNotExist:
            logger.error(f"UserProfile does not exist for user {user.id}")
            return False
