import firebase_admin
from firebase_admin import credentials
import os
from django.conf import settings

def initialize_firebase():
    """A robust way to initialize Firebase, checking if it's already initialized."""
    if not firebase_admin._apps:
        cred_path = settings.FIREBASE_SERVICE_ACCOUNT_KEY_PATH
        if cred_path and os.path.exists(cred_path):
            try:
                cred = credentials.Certificate(cred_path)
                firebase_admin.initialize_app(cred)
                print("Firebase Admin SDK initialized on-demand.")
            except Exception as e:
                print(f"Error initializing Firebase on-demand: {e}")
        else:
            print("WARNING: Firebase service account key path not found. Cannot initialize Firebase.")
