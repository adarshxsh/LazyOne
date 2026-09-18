from django.apps import AppConfig
from .firebase_init import initialize_firebase
import firebase_admin
from firebase_admin import firestore
import logging

logger = logging.getLogger(__name__)

class BasicConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'basic'
    _firestore_db = None

    def ready(self):
        initialize_firebase()

    @property
    def firestore_db(self):
        if self._firestore_db is None:
            try:
                initialize_firebase()
                if firebase_admin._apps:
                    self._firestore_db = firestore.client()
            except Exception as e:
                logger.error(f"Failed to initialize Firestore client: {e}")
                self._firestore_db = None
        return self._firestore_db

