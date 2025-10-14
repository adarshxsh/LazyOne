import firebase_admin
from firebase_admin import credentials
import os
import json

def initialize_firebase():
    """
    A robust, idempotent function to initialize the Firebase Admin SDK.
    It initializes directly from environment variables, avoiding filesystem issues in serverless environments.
    """
    # If the app is already initialized, do nothing.
    if firebase_admin._apps:
        return

    # Get the JSON content from the environment variable
    firebase_json_content = os.getenv('FIREBASE_SERVICE_ACCOUNT_JSON')

    if not firebase_json_content:
        print("WARNING: FIREBASE_SERVICE_ACCOUNT_JSON environment variable not set. Firebase Admin SDK cannot be initialized.")
        return

    try:
        # Parse the JSON string into a dictionary
        cred_dict = json.loads(firebase_json_content)
        
        # Initialize the app using a credentials dictionary
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        print("Firebase Admin SDK initialized successfully from environment variable.")

    except json.JSONDecodeError:
        print("ERROR: Failed to parse FIREBASE_SERVICE_ACCOUNT_JSON. Make sure it is a valid JSON string.")
    except Exception as e:
        print(f"ERROR: An unexpected error occurred during Firebase initialization: {e}")
