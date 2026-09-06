from django.test import TestCase, override_settings
from django.test.client import RequestFactory
from basic.context_processors import firebase_keys
from django.conf import settings

class FirebaseConfigTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @override_settings(
        FIREBASE_API_KEY='test-api-key',
        FIREBASE_AUTH_DOMAIN='test-auth-domain',
        FIREBASE_PROJECT_ID='test-project-id',
        FIREBASE_STORAGE_BUCKET='test-storage-bucket',
        FIREBASE_MESSAGING_SENDER_ID='test-sender-id',
        FIREBASE_APP_ID='test-app-id'
    )
    def test_firebase_keys_context_processor(self):
        """
        Verify that the context processor correctly injects the settings-configured
        Firebase client configuration keys globally.
        """
        request = self.factory.get('/')
        context = firebase_keys(request)
        self.assertEqual(context['FIREBASE_API_KEY'], 'test-api-key')
        self.assertEqual(context['FIREBASE_AUTH_DOMAIN'], 'test-auth-domain')
        self.assertEqual(context['FIREBASE_PROJECT_ID'], 'test-project-id')
        self.assertEqual(context['FIREBASE_STORAGE_BUCKET'], 'test-storage-bucket')
        self.assertEqual(context['FIREBASE_MESSAGING_SENDER_ID'], 'test-sender-id')
        self.assertEqual(context['FIREBASE_APP_ID'], 'test-app-id')

    def test_firebase_keys_defined_in_settings(self):
        """
        Verify that the required Firebase settings variables are defined as string values
        on settings.
        """
        self.assertTrue(hasattr(settings, 'FIREBASE_API_KEY'))
        self.assertTrue(hasattr(settings, 'FIREBASE_AUTH_DOMAIN'))
        self.assertTrue(hasattr(settings, 'FIREBASE_PROJECT_ID'))
        self.assertTrue(hasattr(settings, 'FIREBASE_STORAGE_BUCKET'))
        self.assertTrue(hasattr(settings, 'FIREBASE_MESSAGING_SENDER_ID'))
        self.assertTrue(hasattr(settings, 'FIREBASE_APP_ID'))
