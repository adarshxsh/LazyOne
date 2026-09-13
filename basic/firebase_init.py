import firebase_admin
from firebase_admin import credentials, firestore
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


def update_dispute_firestore(dispute_id, task_id, status, event_type, raised_by_username):
    """
    Writes or updates a document in the 'disputes' Firestore collection.
    Document ID: dispute_id
    Fields: { dispute_id, task_id, status, event_type, raised_by, updated_at }
    """
    try:
        initialize_firebase()
        if firebase_admin._apps:
            db = firestore.client()
            doc_ref = db.collection('disputes').doc(str(dispute_id))
            payload = {
                'dispute_id': dispute_id,
                'task_id': task_id,
                'status': status,
                'event_type': event_type,
                'raised_by': raised_by_username,
                'updated_at': firestore.SERVER_TIMESTAMP
            }
            doc_ref.set(payload, merge=True)
    except Exception as e:
        print(f"ERROR: Failed to update Firestore for dispute {dispute_id}: {e}")

