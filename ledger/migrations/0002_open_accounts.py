"""
Move existing balances into the double-entry ledger.

- Every UserProfile.rewards becomes a USER account funded from the SYSTEM account.
- Every task still holding points (open / in progress / disputed) gets an escrow account
  holding its reward (the poster was already debited when the task was posted).
Afterwards SUM(entries) == 0 and every cached balance matches its entries.
"""
from django.db import migrations

ACTIVE = ('available', 'in_progress', 'submitted', 'disputed')


def forwards(apps, schema_editor):
    Account = apps.get_model('ledger', 'Account')
    Transfer = apps.get_model('ledger', 'Transfer')
    Entry = apps.get_model('ledger', 'Entry')
    UserProfile = apps.get_model('basic', 'UserProfile')
    Task = apps.get_model('basic', 'Task')

    system, _ = Account.objects.get_or_create(key='system:mint', defaults={'kind': 'system'})

    def mint(account, amount, key, reason, description, task=None):
        if amount <= 0 or Transfer.objects.filter(idempotency_key=key).exists():
            return
        t = Transfer.objects.create(idempotency_key=key, reason=reason, description=description, task=task)
        Entry.objects.create(transfer=t, account=system, amount=-amount)
        Entry.objects.create(transfer=t, account=account, amount=amount)
        system.balance -= amount
        account.balance += amount
        account.save(update_fields=['balance'])

    for p in UserProfile.objects.all():
        acc, created = Account.objects.get_or_create(key=f'user:{p.user_id}', defaults={'kind': 'user', 'user_id': p.user_id})
        if created:
            mint(acc, p.rewards, f'user:{p.user_id}:open', 'initial_points', 'Opening balance (migrated)')

    for t in Task.objects.filter(status__in=ACTIVE):
        acc, created = Account.objects.get_or_create(key=f'task:{t.pk}', defaults={'kind': 'task_escrow'})
        if created:
            mint(acc, t.reward, f'task:{t.pk}:fund', 'task_creation', f"Escrow for '{t.title}' (migrated)", task=t)

    system.save(update_fields=['balance'])


class Migration(migrations.Migration):
    dependencies = [
        ('ledger', '0001_initial'),
        ('basic', '0003_v2_backfill'),
    ]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
