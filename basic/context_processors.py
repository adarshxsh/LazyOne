import os
from django.conf import settings

def firebase_keys(request):
    """
    Returns a dictionary of Firebase client-side configuration keys.
    """
    return {
        'FIREBASE_API_KEY': getattr(settings, 'FIREBASE_API_KEY', os.getenv('FIREBASE_API_KEY', '')),
        'FIREBASE_AUTH_DOMAIN': getattr(settings, 'FIREBASE_AUTH_DOMAIN', os.getenv('FIREBASE_AUTH_DOMAIN', '')),
        'FIREBASE_PROJECT_ID': getattr(settings, 'FIREBASE_PROJECT_ID', os.getenv('FIREBASE_PROJECT_ID', '')),
        'FIREBASE_STORAGE_BUCKET': getattr(settings, 'FIREBASE_STORAGE_BUCKET', os.getenv('FIREBASE_STORAGE_BUCKET', '')),
        'FIREBASE_MESSAGING_SENDER_ID': getattr(settings, 'FIREBASE_MESSAGING_SENDER_ID', os.getenv('FIREBASE_MESSAGING_SENDER_ID', '')),
        'FIREBASE_APP_ID': getattr(settings, 'FIREBASE_APP_ID', os.getenv('FIREBASE_APP_ID', '')),
    }

def unread_notifications_count(request):
    """
    Returns the number of unread notifications for the current user.
    """
    if request.user.is_authenticated:
        count = request.user.notifications.filter(is_read=False).count()
        return {'unread_notifications_count': count}
    return {'unread_notifications_count': 0}
