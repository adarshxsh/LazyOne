import random
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.db.models import Q
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.utils import timezone
from django.contrib.auth.models import User
from ..models import Dispute, Task, Notification, UserProfile, Friendship, RewardLedger, JuryAssignment, DisputeVote

def get_excluded_juror_user_ids(task):
    excluded_ids = set()
    litigants = []
    if task.posted_by:
        litigants.append(task.posted_by)
    if task.taken_by:
        litigants.append(task.taken_by)

    for user in litigants:
        excluded_ids.add(user.id)
        try:
            profile = user.userprofile
        except UserProfile.DoesNotExist:
            continue

        # Direct friends in ManyToMany field
        direct_friends = profile.friends.all().values_list('user_id', flat=True)
        excluded_ids.update(direct_friends)

        # Friends via Friendship model
        from_friends = Friendship.objects.filter(from_user=profile).values_list('to_user__user_id', flat=True)
        excluded_ids.update(from_friends)

        to_friends = Friendship.objects.filter(to_user=profile).values_list('from_user__user_id', flat=True)
        excluded_ids.update(to_friends)

    return excluded_ids

def draw_dispute_jurors(dispute, stake_amount=100, jury_size=3):
    excluded_ids = get_excluded_juror_user_ids(dispute.task)

    candidates = list(User.objects.filter(
        is_active=True,
        userprofile__rewards__gte=stake_amount
    ).exclude(id__in=excluded_ids))

    if len(candidates) < jury_size:
        dispute.status = 'pending_juror'
        dispute.save()

        admins = User.objects.filter(Q(is_staff=True) | Q(is_superuser=True)).distinct()
        for admin in admins:
            Notification.objects.create(
                recipient=admin,
                message=f"Dispute for task '{dispute.task.title}' is pending juror draw due to insufficient eligible jurors.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        return False, []

    selected_jurors = random.sample(candidates, jury_size)
    with transaction.atomic():
        for juror in selected_jurors:
            juror_profile = juror.userprofile
            juror_profile.rewards -= stake_amount
            juror_profile.save()

            RewardLedger.objects.create(
                user=juror,
                task=dispute.task,
                amount=-stake_amount,
                transaction_type='juror_stake_lock',
                description=f"Jury stake locked for dispute on task: '{dispute.task.title}'"
            )

            JuryAssignment.objects.create(
                dispute=dispute,
                juror=juror,
                staked_amount=stake_amount,
                stake_status='locked',
                is_staked=True,
                has_voted=False
            )

            Notification.objects.create(
                recipient=juror,
                message=f"You have been assigned as a juror for dispute: '{dispute.task.title}'. A stake of {stake_amount} points has been reserved.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    return True, selected_jurors

def release_juror_stakes(dispute):
    assignments = JuryAssignment.objects.filter(dispute=dispute, is_staked=True)
    with transaction.atomic():
        for assignment in assignments:
            juror = assignment.juror
            try:
                juror_profile = juror.userprofile
            except UserProfile.DoesNotExist:
                juror_profile = UserProfile.objects.create(user=juror)

            juror_profile.rewards += assignment.staked_amount
            juror_profile.save()

            RewardLedger.objects.create(
                user=juror,
                task=dispute.task,
                amount=assignment.staked_amount,
                transaction_type='juror_stake_release',
                description=f"Jury stake refunded for dispute on task: '{dispute.task.title}'"
            )

            assignment.stake_status = 'released'
            assignment.is_staked = False
            assignment.save()

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    is_assigned_juror = JuryAssignment.objects.filter(dispute=dispute, juror=request.user).exists()
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff and not is_assigned_juror:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')
    
    context = {
        'dispute': dispute,
        'task': task,
        'is_assigned_juror': is_assigned_juror,
        'jury_assignments': dispute.jury_assignments.all(),
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute'):
        return redirect('dispute_detail', dispute_id=task.dispute.id)
    if task.taken_by != request.user or task.status != 'in_progress':
        messages.error(request, "You can only raise a dispute for a task you have taken that is currently in progress.")
        return redirect('my_tasks')
    if request.method == 'POST':
        reason = request.POST.get('reason')
        if not reason:
            messages.error(request, "A reason is required to raise a dispute.")
            return redirect('my_tasks')
        
        with transaction.atomic():
            dispute = Dispute.objects.create(task=task, raised_by=request.user, reason=reason)
            task.status = 'disputed'
            task.save()
            Notification.objects.create(
                recipient=task.posted_by,
                message=f"{request.user.username} has raised a dispute for your task: '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            draw_dispute_jurors(dispute)

        messages.success(request, "Dispute raised successfully.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    task = dispute.task
    with transaction.atomic():
        task.status = 'in_progress'
        task.save()
        release_juror_stakes(dispute)
        dispute.delete()
        Notification.objects.create(
            recipient=task.posted_by,
            message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
            link=reverse('my_tasks')
        )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'.")
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def submit_dispute_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    assignment = get_object_or_404(JuryAssignment, dispute=dispute, juror=request.user)
    choice = request.POST.get('choice')
    if choice not in ['poster', 'taker']:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    
    with transaction.atomic():
        vote, created = DisputeVote.objects.get_or_create(
            dispute=dispute,
            voter=request.user,
            defaults={'choice': choice}
        )
        if not created:
            messages.info(request, "You have already voted on this dispute.")
            return redirect('dispute_detail', dispute_id=dispute.id)
        
        assignment.has_voted = True
        assignment.voted_at = timezone.now()
        assignment.save()
        
        total_votes = dispute.votes.count()
        if total_votes >= 3:
            poster_votes = dispute.votes.filter(choice='poster').count()
            taker_votes = dispute.votes.filter(choice='taker').count()
            
            dispute.status = 'resolved'
            dispute.save()
            
            task = dispute.task
            if taker_votes > poster_votes:
                task.status = 'completed'
                task.save()
                task_doer_profile = task.taken_by.userprofile
                task_doer_profile.rewards += task.reward
                task_doer_profile.save()
                RewardLedger.objects.create(
                    user=task.taken_by, task=task, amount=task.reward,
                    transaction_type='task_completion', description=f"Completed task via dispute resolution: '{task.title}'"
                )
            else:
                task.status = 'cancelled'
                task.save()
                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += task.reward
                poster_profile.save()
                RewardLedger.objects.create(
                    user=task.posted_by, task=task, amount=task.reward,
                    transaction_type='task_cancellation', description=f"Refund for cancelled task via dispute resolution: '{task.title}'"
                )
            
            release_juror_stakes(dispute)
            messages.success(request, "Vote recorded and dispute resolved.")
        else:
            messages.success(request, "Your vote has been recorded.")
            
    return redirect('dispute_detail', dispute_id=dispute.id)
