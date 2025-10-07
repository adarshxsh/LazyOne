from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from ..models import RewardLedger, Task
from django.db.models import Sum, Q

@login_required(login_url='/login/')
def rewards_view(request):
    user = request.user

    # Get all transactions for the user
    all_transactions = RewardLedger.objects.filter(user=user).order_by('-created_at')

    # Calculate different categories
    rewards_earned = all_transactions.filter(amount__gt=0) # Positive amounts are earned
    rewards_given = all_transactions.filter(amount__lt=0, transaction_type='task_creation') # Negative amounts for creating tasks

    # Calculate pending points for tasks that are 'in_progress'
    pending_tasks = Task.objects.filter(posted_by=user, status='in_progress')
    pending_points = pending_tasks.aggregate(Sum('reward'))['reward__sum'] or 0

    context = {
        'all_transactions': all_transactions,
        'rewards_earned': rewards_earned,
        'rewards_given': rewards_given,
        'pending_points': pending_points,
        'current_balance': user.userprofile.rewards
    }
    return render(request, 'rewards.html', context)
