from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender='basic.UserProfile')
def open_wallet(sender, instance, created, **kwargs):
    if created:
        from .services import user_account
        user_account(instance.user)
