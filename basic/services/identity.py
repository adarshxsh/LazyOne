"""
Identity (ARCHITECTURE_V2 §4.7): users are keyed by the verified Firebase uid.
Email is an attribute, used once to link a pre-existing account.
"""
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction

from ..models import Community, UserProfile


def user_from_firebase_claims(claims):
    uid = claims.get('uid') or claims.get('sub')
    if not uid:
        return None
    profile = UserProfile.objects.select_related('user').filter(firebase_uid=uid).first()
    if profile:
        return profile.user

    email = (claims.get('email') or '').lower()
    with transaction.atomic():
        user = None
        if email and claims.get('email_verified'):
            user = User.objects.filter(email__iexact=email).first()
        if user is None:
            username = email or f"fb_{uid}"
            if User.objects.filter(username=username).exists():
                username = f"fb_{uid}"
            try:
                user = User.objects.create(username=username[:150], email=email)
            except IntegrityError:
                user = User.objects.get(username=username[:150])
        profile, _ = UserProfile.objects.get_or_create(
            user=user, defaults={'community': Community.for_email(email)})
        if not profile.firebase_uid:
            profile.firebase_uid = uid
            profile.save(update_fields=['firebase_uid'])
        elif profile.firebase_uid != uid:
            return None  # email belongs to an account bound to another Firebase identity
    return user
