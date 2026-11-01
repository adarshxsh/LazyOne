"""
The only code allowed to move points. See ARCHITECTURE_V2 §4.1.

    transfer([(from_account, -100), (to_account, +100)], key="task:12:payout", reason=...)

- Amounts must sum to zero.
- Accounts are locked in id order (no deadlocks) and balances are re-checked under the lock.
- The idempotency key makes every logical movement happen at most once: calling
  transfer() again with the same key returns the original Transfer and moves nothing.
"""
from django.db import IntegrityError, transaction
from django.db.models import Sum

from .models import Account, Entry, Transfer

SYSTEM_MINT = 'system:mint'


class InsufficientFunds(Exception):
    def __init__(self, account, needed):
        self.account = account
        self.needed = needed
        super().__init__(f"{account.key} has {account.balance}, needs {needed}")


def _get_or_create(key, kind, **extra):
    try:
        return Account.objects.get_or_create(key=key, defaults={'kind': kind, **extra})[0]
    except IntegrityError:  # concurrent creation
        return Account.objects.get(key=key)


def system_account():
    return _get_or_create(SYSTEM_MINT, Account.Kind.SYSTEM)


def task_escrow(task):
    return _get_or_create(f"task:{task.pk}", Account.Kind.TASK_ESCROW)


def dispute_bond(dispute):
    return _get_or_create(f"dispute:{dispute.pk}", Account.Kind.DISPUTE_BOND)


def user_account(user):
    """
    The user's wallet. Opened on first use by minting the user's current
    UserProfile.rewards (sign-up bonus, or legacy balance from before the ledger).
    """
    try:
        return Account.objects.get(key=f"user:{user.pk}")
    except Account.DoesNotExist:
        pass
    account = _get_or_create(f"user:{user.pk}", Account.Kind.USER, user=user)
    from basic.models import UserProfile
    opening = UserProfile.objects.filter(user=user).values_list('rewards', flat=True).first() or 0
    if opening:
        transfer(
            [(system_account(), -opening), (account, opening)],
            key=f"user:{user.pk}:open", reason=Transfer.Reason.INITIAL_POINTS,
            description="Initial points",
        )
        account.refresh_from_db()
    return account


def balance_of(user):
    return user_account(user).balance


def transfer(legs, *, key, reason, description='', task=None):
    """Apply a balanced set of (account, amount) legs atomically and at most once."""
    legs = [(acc, int(amount)) for acc, amount in legs if int(amount) != 0]
    if not legs:
        raise ValueError("A transfer needs at least one non-zero leg.")
    if sum(amount for _, amount in legs) != 0:
        raise ValueError("Transfer legs must sum to zero.")

    with transaction.atomic():
        existing = Transfer.objects.filter(idempotency_key=key).first()
        if existing:
            return existing

        ids = sorted({acc.pk for acc, _ in legs})
        locked = {a.pk: a for a in Account.objects.select_for_update().filter(pk__in=ids).order_by('pk')}

        deltas = {}
        for acc, amount in legs:
            deltas[acc.pk] = deltas.get(acc.pk, 0) + amount
        for pk, delta in deltas.items():
            acc = locked[pk]
            if acc.kind != Account.Kind.SYSTEM and acc.balance + delta < 0:
                raise InsufficientFunds(acc, -delta)

        try:
            with transaction.atomic():
                t = Transfer.objects.create(idempotency_key=key, reason=reason,
                                            description=description[:255], task=task)
        except IntegrityError:
            # Lost a race on the same key: the other transfer won, nothing to do.
            return Transfer.objects.get(idempotency_key=key)

        Entry.objects.bulk_create([Entry(transfer=t, account_id=acc.pk, amount=amount) for acc, amount in legs])
        for pk, delta in deltas.items():
            acc = locked[pk]
            acc.balance += delta
            acc.save(update_fields=['balance'])
            if acc.kind == Account.Kind.USER:
                _mirror_user(acc, delta, t)
        return t


def _mirror_user(account, delta, t):
    """Keep UserProfile.rewards and the legacy RewardLedger history in sync for the UI."""
    from basic.models import RewardLedger, UserProfile
    UserProfile.objects.filter(user_id=account.user_id).update(rewards=account.balance)
    RewardLedger.objects.create(
        user_id=account.user_id, task=t.task, amount=delta,
        transaction_type=t.reason, description=t.description or t.get_reason_display(),
    )


def check_invariants():
    """Return a list of problems; empty means the ledger is consistent."""
    problems = []
    total = Entry.objects.aggregate(s=Sum('amount'))['s'] or 0
    if total != 0:
        problems.append(f"Entries sum to {total}, expected 0")
    sums = dict(Entry.objects.values('account').annotate(s=Sum('amount')).values_list('account', 's'))
    for acc in Account.objects.all():
        if sums.get(acc.pk, 0) != acc.balance:
            problems.append(f"{acc.key}: cached balance {acc.balance} != entries {sums.get(acc.pk, 0)}")
    from basic.models import UserProfile
    for p in UserProfile.objects.select_related('user__ledger_account').all():
        acc = getattr(p.user, 'ledger_account', None)
        if acc and acc.balance != p.rewards:
            problems.append(f"user:{p.user_id}: profile.rewards {p.rewards} != ledger {acc.balance}")
    return problems
