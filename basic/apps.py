from django.apps import AppConfig
from .firebase_init import initialize_firebase
import firebase_admin
from firebase_admin import firestore

class MockFirestoreDb:
    def collection(self, *args, **kwargs):
        return self
    def document(self, *args, **kwargs):
        return self
    def set(self, *args, **kwargs):
        pass
    def get(self, *args, **kwargs):
        return None

class BasicConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'basic'

    def ready(self):
        initialize_firebase()
        if firebase_admin._apps:
            try:
                self.firestore_db = firestore.client()
            except Exception:
                self.firestore_db = MockFirestoreDb()
        else:
            self.firestore_db = MockFirestoreDb()
