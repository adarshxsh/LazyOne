"""
Realtime fan-out (ARCHITECTURE_V2 §4.4). Postgres is the source of truth; Firestore is a
server-written, client-read-only mirror. Swap the publisher to move to Channels/SSE later.
"""
import logging

import firebase_admin

logger = logging.getLogger(__name__)


class NullPublisher:
    enabled = False

    def set_conversation_members(self, conversation_public_id, member_uids):
        pass

    def publish_message(self, conversation_public_id, message_doc):
        pass


class FirestorePublisher:
    enabled = True

    def __init__(self):
        from firebase_admin import firestore
        self._fs = firestore
        self.db = firestore.client()

    def set_conversation_members(self, conversation_public_id, member_uids):
        self.db.collection('conversations').document(conversation_public_id).set(
            {'member_uids': sorted(member_uids)}, merge=True)

    def publish_message(self, conversation_public_id, message_doc):
        doc = dict(message_doc, timestamp=self._fs.SERVER_TIMESTAMP)
        (self.db.collection('conversations').document(conversation_public_id)
         .collection('messages').document(str(message_doc['id'])).set(doc))


_publisher = None


def get_publisher():
    global _publisher
    if _publisher is None:
        from django.conf import settings
        enabled = getattr(settings, 'REALTIME_ENABLED', True) and firebase_admin._apps
        _publisher = FirestorePublisher() if enabled else NullPublisher()
    return _publisher


def create_custom_token(user):
    """Firebase custom token so the browser/app can read Firestore under Django's identity."""
    from django.conf import settings
    if not firebase_admin._apps or not getattr(settings, 'REALTIME_ENABLED', True):
        return None
    from firebase_admin import auth
    from .models import realtime_uid
    token = auth.create_custom_token(realtime_uid(user))
    return token.decode() if isinstance(token, bytes) else token
