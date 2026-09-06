from django.apps import AppConfig

class BasicConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'basic'
    firestore_db = None

    def ready(self):
        try:
            from .firebase_init import initialize_firebase
            initialize_firebase()
            
            import firebase_admin
            from firebase_admin import firestore
            if firebase_admin._apps:
                self.firestore_db = firestore.client()
                print("Firestore client initialized successfully on AppConfig.")
            else:
                self.firestore_db = None
        except Exception as e:
            print(f"Warning: Could not initialize firestore_db on AppConfig: {e}")
            self.firestore_db = None
