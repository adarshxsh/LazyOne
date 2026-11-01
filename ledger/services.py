"""
The only code allowed to move points. See ARCHITECTURE_V2 §4.1.

    transfer([(from_account, -100), (to_account, +100)], key="task:12:payout", reason=...)

- Amounts must sum to zero.
- Accounts are locked in id order (no deadlocks) and balances are re-checked under the lock.
- The idempotency key makes every logical movement happen at most once: calling
  transfer() again with the same key returns the original Transfer and moves nothing.
"""
from django.conf import settings
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


def _is_verified(profile):
    """Phone verified, or member of a campus community (email domain checked at sign-up)."""
    return bool(profile and (profile.is_phone_verified or (profile.community_id and profile.community.email_domain)))


def bonus_hold(user):
    """Sign-up bonus held until the user verifies (anti fake-account farming)."""
    return _get_or_create(f"bonus_hold:{user.pk}", Account.Kind.SYSTEM)


def user_account(user):
    """
    The user's wallet. Opened on first use by minting the user's current
    UserProfile.rewards (sign-up bonus, or legacy balance from before the ledger).

    When SIGNUP_BONUS_REQUIRES_VERIFICATION is on and the user isn't verified yet, only
    UNVERIFIED_STARTER_POINTS go to the wallet; the rest waits in bonus_hold:<id> and is
    released by release_bonus() once the user verifies.
    """
    try:
        return Account.objects.get(key=f"user:{user.pk}")
    except Account.DoesNotExist:
        pass
    account = _get_or_create(f"user:{user.pk}", Account.Kind.USER, user=user)
    from basic.models import UserProfile
    profile = UserProfile.objects.select_related('community').filter(user=user).first()
    opening = (profile.rewards if profile else 0) or 0
    if opening:
        cfg = settings.LAZYONE
        held = 0
        if cfg.get('SIGNUP_BONUS_REQUIRES_VERIFICATION') and not _is_verified(profile):
            held = max(0, opening - cfg.get('UNVERIFIED_STARTER_POINTS', 0))
        legs = [(system_account(), -opening), (account, opening - held)]
        if held:
            legs.append((bonus_hold(user), held))
        transfer(legs, key=f"user:{user.pk}:open", reason=Transfer.Reason.INITIAL_POINTS,
                 description="Initial points" + (f" ({held} held until verified)" if held else ""))
        account.refresh_from_db()
    return account


def release_bonus(user):
    """Move a held sign-up bonus to the wallet once the user is verified. Safe to call repeatedly."""
    from basic.models import UserProfile
    profile = UserProfile.objects.select_related('community').filter(user=user).first()
    if not _is_verified(profile):
        return 0
    hold = Account.objects.filter(key=f"bonus_hold:{user.pk}").first()
    if not hold or hold.balance <= 0:
        return 0
    amount = hold.balance
    transfer([(hold, -amount), (user_account(user), amount)], key=f"user:{user.pk}:bonus_release",
             reason=Transfer.Reason.INITIAL_POINTS, description="Sign-up bonus released after verification")
    return amount


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
