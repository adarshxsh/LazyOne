from django.apps import AppConfig
import firebase_admin
from firebase_admin import credentials, firestore
import os
from django.conf import settings # Import settings

class BasicConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'basic'
    firestore_db = None # Class attribute to hold the Firestore client

    def ready(self):
        # This method is called once Django is ready
        if settings.configured:
            # Use the path from settings, which is correctly configured for Vercel
            cred_path = settings.FIREBASE_SERVICE_ACCOUNT_KEY_PATH

            if not firebase_admin._apps:
                if cred_path and os.path.exists(cred_path):
                    try:
                        cred = credentials.Certificate(cred_path)
                        firebase_admin.initialize_app(cred)
                        print("Firebase Admin SDK initialized successfully in apps.py.")
                        self.firestore_db = firestore.client()
                    except Exception as e:
                        print(f"Error initializing Firebase Admin SDK in apps.py: {e}")
                        self.firestore_db = None
                else:
                    # This warning will now correctly trigger if the file written in settings.py is not found
                    print("WARNING: Firebase service account key path not found or file does not exist. Backend Firebase features will be disabled.")
                    self.firestore_db = None
            else:
                self.firestore_db = firestore.client()
        else:
            print("WARNING: Django settings not configured when BasicConfig.ready() was called. Firebase initialization skipped.")
