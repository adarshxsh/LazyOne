"""
Double-entry points ledger (ARCHITECTURE_V2 §4.1).

Every movement of points is a Transfer made of Entries whose amounts sum to zero.
Account.balance is a cache of SUM(entries) maintained in the same transaction.
Only the SYSTEM account may go negative (it is where sign-up points are minted from).
"""
from django.conf import settings
from django.db import models


class Account(models.Model):
    class Kind(models.TextChoices):
        USER = 'user', 'User wallet'
        TASK_ESCROW = 'task_escrow', 'Task escrow'
        DISPUTE_BOND = 'dispute_bond', 'Dispute bond'
        SYSTEM = 'system', 'System'

    key = models.CharField(max_length=64, unique=True)  # e.g. "user:5", "task:12", "dispute:3", "system:mint"
    kind = models.CharField(max_length=20, choices=Kind.choices)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, null=True, blank=True,
                                on_delete=models.PROTECT, related_name='ledger_account')
    balance = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(kind='system') | models.Q(balance__gte=0),
                name='ledger_account_non_negative',
            ),
        ]

    def __str__(self):
        return f"{self.key} ({self.balance})"


class Transfer(models.Model):
    class Reason(models.TextChoices):
        INITIAL_POINTS = 'initial_points', 'Initial points'
        TASK_FUND = 'task_creation', 'Task escrow funded'
        TASK_PAYOUT = 'task_completion', 'Task payout'
        TASK_REFUND = 'task_cancellation', 'Task refund'
        DISPUTE_BOND = 'dispute_bond', 'Dispute bond posted'
        DISPUTE_BOND_REFUND = 'dispute_bond_refund', 'Dispute bond refunded'
        DISPUTE_BOND_FORFEIT = 'dispute_bond_forfeit', 'Dispute bond forfeited'

    idempotency_key = models.CharField(max_length=128, unique=True)
    reason = models.CharField(max_length=30, choices=Reason.choices)
    description = models.CharField(max_length=255, blank=True)
    task = models.ForeignKey('basic.Task', null=True, blank=True, on_delete=models.SET_NULL, related_name='transfers')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self):
        return f"{self.reason} [{self.idempotency_key}]"


class Entry(models.Model):
    transfer = models.ForeignKey(Transfer, on_delete=models.PROTECT, related_name='entries')
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='entries')
    amount = models.BigIntegerField()

    class Meta:
        indexes = [models.Index(fields=['account', 'id'])]

    def __str__(self):
        return f"{self.account.key} {self.amount:+d}"
