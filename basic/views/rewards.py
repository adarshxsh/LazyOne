from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from ..models import RewardLedger, Task, UserProfile
from django.db.models import Sum

@login_required(login_url='/login/')
def rewards_view(request):
    user = request.user
    currency_filter = request.GET.get('currency', '')

    # Get all transactions for the user
    all_transactions = RewardLedger.objects.filter(user=user).order_by('-created_at')

    if currency_filter in ['points', 'usd']:
        transactions = all_transactions.filter(currency=currency_filter)
    else:
        transactions = all_transactions

    # Calculate stats for Points
    points_earned = all_transactions.filter(currency='points', amount__gt=0).aggregate(Sum('amount'))['amount__sum'] or 0
    points_given = all_transactions.filter(currency='points', amount__lt=0, transaction_type='task_creation').aggregate(Sum('amount'))['amount__sum'] or 0
    points_given = abs(points_given)
    pending_tasks_points = Task.objects.filter(posted_by=user, status='in_progress', reward_type='points')
    pending_points = pending_tasks_points.aggregate(Sum('reward'))['reward__sum'] or 0
    current_points_balance = user.userprofile.rewards

    # Calculate stats for USD (ledger amounts are in cents, Task rewards are in dollars)
    usd_earned_cents = all_transactions.filter(currency='usd', amount__gt=0).aggregate(Sum('amount'))['amount__sum'] or 0
    usd_earned = usd_earned_cents / 100.0

    usd_spent_cents = all_transactions.filter(currency='usd', amount__lt=0, transaction_type='task_creation').aggregate(Sum('amount'))['amount__sum'] or 0
    usd_spent = abs(usd_spent_cents) / 100.0

    # Escrowed USD holds (posted by user, not completed/cancelled yet)
    usd_escrow_tasks = Task.objects.filter(posted_by=user, status__in=['available', 'in_progress', 'disputed'], reward_type='usd')
    usd_escrow_hold = usd_escrow_tasks.aggregate(Sum('reward'))['reward__sum'] or 0

    user_profile, _ = UserProfile.objects.get_or_create(user=user)
    stripe_connected = bool(user_profile.stripe_account_id)

    context = {
        'all_transactions': transactions,
        'currency_filter': currency_filter,
        'points_earned': points_earned,
        'points_given': points_given,
        'pending_points': pending_points,
        'current_balance': current_points_balance,
        'usd_earned': usd_earned,
        'usd_spent': usd_spent,
        'usd_escrow_hold': usd_escrow_hold,
        'stripe_connected': stripe_connected,
    }
    return render(request, 'rewards.html', context)
