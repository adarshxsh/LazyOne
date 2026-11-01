from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender='basic.UserProfile')
def open_wallet(sender, instance, created, **kwargs):
    from .models import Account
    from .services import release_bonus, user_account
    if created:
        # Keep the in-memory instance in sync so a later save() can't restore the full bonus.
        instance.rewards = user_account(instance.user).balance
        return
    if instance.is_phone_verified:
        release_bonus(instance.user)  # no-op unless a bonus is still held
    # UserProfile.rewards is a display copy of the ledger; a save() from a stale instance
    # must never change it.
    balance = Account.objects.filter(key=f"user:{instance.user_id}").values_list('balance', flat=True).first()
    if balance is not None and balance != instance.rewards:
        type(instance).objects.filter(pk=instance.pk).update(rewards=balance)
        instance.rewards = balance
