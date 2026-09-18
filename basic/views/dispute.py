import random
from datetime import timedelta
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.contrib.auth.models import User
from django.db.models import Q
from ..models import Dispute, Task, Notification, DisputeJuror, DisputeVote, RewardLedger, UserProfile

def select_neutral_jurors(task, panel_size=3):
    """
    Selects an odd-numbered panel of neutral community members.
    Filters out task counterparties, direct friends of counterparties,
    and conversation participants.
    """
    excluded_user_ids = set()

    # Counterparties
    if task.posted_by_id:
        excluded_user_ids.add(task.posted_by_id)
        if hasattr(task.posted_by, 'userprofile'):
            poster_friends = task.posted_by.userprofile.friends.values_list('user__id', flat=True)
            excluded_user_ids.update(poster_friends)

    if task.taken_by_id:
        excluded_user_ids.add(task.taken_by_id)
        if hasattr(task.taken_by, 'userprofile'):
            worker_friends = task.taken_by.userprofile.friends.values_list('user__id', flat=True)
            excluded_user_ids.update(worker_friends)

    # Conversation participants
    if hasattr(task, 'conversation') and task.conversation:
        conv_participants = task.conversation.participants.values_list('id', flat=True)
        excluded_user_ids.update(conv_participants)

    # Base active candidates
    candidates_qs = User.objects.filter(is_active=True).exclude(id__in=excluded_user_ids)

    # Filter for verified active users if enough exist
    verified_candidates = candidates_qs.filter(
        Q(userprofile__is_phone_verified=True) | Q(userprofile__is_instagram_verified=True)
    )

    if verified_candidates.count() >= panel_size:
        eligible_pool = list(verified_candidates)
    else:
        eligible_pool = list(candidates_qs)

    if len(eligible_pool) <= panel_size:
        return eligible_pool

    return random.sample(eligible_pool, panel_size)


def resolve_dispute(dispute, winning_party):
    """
    Executes dispute resolution:
    - Sets dispute status and outcome
    - Transfers/refunds task reward points
    - Awards juror incentive points to all participating jurors who cast a vote
    - Creates RewardLedger entries
    - Sends notifications
    """
    if dispute.status != 'open':
        return

    with transaction.atomic():
        dispute.status = 'resolved'
        dispute.resolution = winning_party
        dispute.save()

        task = dispute.task
        if winning_party == 'poster_wins':
            task.status = 'cancelled'
            task.save()
            # Refund poster
            if hasattr(task.posted_by, 'userprofile'):
                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += task.reward
                poster_profile.save()
            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Refund for task '{task.title}' following peer jury dispute resolution."
            )
            if dispute.raised_by == task.posted_by:
                dispute.refund_deposit()
            else:
                dispute.forfeit_deposit(beneficiary=task.posted_by)
        elif winning_party == 'worker_wins':
            task.status = 'completed'
            task.save()
            if task.taken_by:
                if hasattr(task.taken_by, 'userprofile'):
                    worker_profile = task.taken_by.userprofile
                    worker_profile.rewards += task.reward
                    worker_profile.save()
                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Reward for task '{task.title}' awarded by peer jury dispute resolution."
                )
            if dispute.raised_by == task.taken_by:
                dispute.refund_deposit()
            else:
                dispute.forfeit_deposit(beneficiary=task.taken_by)

        # Incentive fee for participating jurors
        juror_incentive = max(20, int(task.reward * 0.10)) if task.reward else 50
        participating_votes = dispute.votes.all()
        for vote_record in participating_votes:
            juror = vote_record.juror
            if hasattr(juror, 'userprofile'):
                juror_profile = juror.userprofile
                juror_profile.rewards += juror_incentive
                juror_profile.save()
            RewardLedger.objects.create(
                user=juror,
                task=task,
                amount=juror_incentive,
                transaction_type='juror_reward',
                description=f"Incentive reward for participating in peer jury adjudication on task '{task.title}'."
            )

        # Notify counterparties
        winner_text = "Task Poster" if winning_party == 'poster_wins' else "Task Worker"
        for party in [task.posted_by, task.taken_by]:
            if party:
                Notification.objects.create(
                    recipient=party,
                    message=f"Peer jury panel reached consensus on task '{task.title}': {winner_text} wins.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )


def evaluate_dispute(dispute):
    """
    Evaluates votes for simple majority consensus or voting deadline expiration.
    """
    if dispute.status != 'open':
        return dispute

    total_jurors = dispute.juror_assignments.count()
    if total_jurors == 0:
        if dispute.voting_deadline and timezone.now() >= dispute.voting_deadline:
            dispute.status = 'unresolved'
            dispute.resolution = 'unresolved'
            dispute.save()
        return dispute

    majority_threshold = (total_jurors // 2) + 1
    poster_votes = dispute.votes.filter(vote='poster_wins').count()
    worker_votes = dispute.votes.filter(vote='worker_wins').count()

    if poster_votes >= majority_threshold:
        resolve_dispute(dispute, 'poster_wins')
    elif worker_votes >= majority_threshold:
        resolve_dispute(dispute, 'worker_wins')
    elif dispute.voting_deadline and timezone.now() >= dispute.voting_deadline:
        # Voting deadline expired
        if poster_votes > worker_votes:
            resolve_dispute(dispute, 'poster_wins')
        elif worker_votes > poster_votes:
            resolve_dispute(dispute, 'worker_wins')
        else:
            dispute.status = 'unresolved'
            dispute.resolution = 'unresolved'
            dispute.save()
            for party in [dispute.task.posted_by, dispute.task.taken_by]:
                if party:
                    Notification.objects.create(
                        recipient=party,
                        message=f"Peer jury for dispute on task '{dispute.task.title}' expired without majority consensus. Escalated to administrative review.",
                        link=reverse('dispute_detail', args=[dispute.id])
                    )
    return dispute


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_counterparty = (request.user == task.posted_by or request.user == task.taken_by)
    is_assigned_juror = DisputeJuror.objects.filter(dispute=dispute, juror=request.user).exists()

    if not is_counterparty and not is_assigned_juror and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    evaluate_dispute(dispute)
    dispute.refresh_from_db()

    total_jurors = dispute.juror_assignments.count()
    votes_cast = dispute.votes.count()

    context = {
        'dispute': dispute,
        'task': task,
        'total_jurors': total_jurors,
        'votes_cast': votes_cast,
        'is_counterparty': is_counterparty,
        'is_assigned_juror': is_assigned_juror,
    }
    return render(request, 'dispute_detail.html', context)


@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status == 'open':
        return redirect('dispute_detail', dispute_id=task.dispute.id)

    if task.taken_by != request.user or task.status != 'in_progress':
        messages.error(request, "You can only raise a dispute for a task you have taken that is currently in progress.")
        return redirect('my_tasks')

    if request.method == 'POST':
        reason = request.POST.get('reason')
        if not reason:
            messages.error(request, "A reason is required to raise a dispute.")
            return redirect('my_tasks')

        deposit_amount = task.deposit_bond_amount
        user_profile = request.user.userprofile
        if user_profile.rewards < deposit_amount:
            messages.error(
                request,
                f"Insufficient reward points balance. You need at least {deposit_amount} points as a deposit bond to raise a dispute, but you only have {user_profile.rewards} points."
            )
            return redirect('my_tasks')

        voting_deadline = timezone.now() + timedelta(hours=48)

        with transaction.atomic():
            user_profile.rewards -= deposit_amount
            user_profile.save()

            if hasattr(task, 'dispute'):
                dispute = task.dispute
                dispute.raised_by = request.user
                dispute.reason = reason
                dispute.status = 'open'
                dispute.deposit_amount = deposit_amount
                dispute.escrow_status = 'held'
                dispute.voting_deadline = voting_deadline
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    deposit_amount=deposit_amount,
                    escrow_status='held',
                    voting_deadline=voting_deadline,
                    status='open'
                )

            RewardLedger.objects.create(
                user=request.user,
                task=task,
                amount=-deposit_amount,
                transaction_type='dispute_deposit',
                description=f"Security deposit bond held for dispute on task: '{task.title}'"
            )

            task.status = 'disputed'
            task.save()

            # Select neutral jury panel (3 members)
            jurors = select_neutral_jurors(task, panel_size=3)
            for juror in jurors:
                DisputeJuror.objects.create(dispute=dispute, juror=juror)
                Notification.objects.create(
                    recipient=juror,
                    message=f"You have been selected as a neutral peer juror for dispute on task '{task.title}'.",
                    link=reverse('dispute_adjudicate', args=[dispute.id])
                )

            # Notify counterparties
            Notification.objects.create(
                recipient=task.posted_by,
                message=f"{request.user.username} has raised a dispute for your task: '{task.title}'. A neutral peer jury panel has been impaneled.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond. A peer jury panel of {len(jurors)} neutral members has been assigned.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    return redirect('my_tasks')


@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    task = dispute.task
    with transaction.atomic():
        dispute.refund_deposit(
            reason_description=f"Security deposit bond refunded for withdrawn dispute on task: '{task.title}'"
        )
        dispute.status = 'resolved'
        dispute.save()

        task.status = 'in_progress'
        task.save()

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
            link=reverse('my_tasks')
        )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')


@login_required(login_url='/login/')
def dispute_adjudicate_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    juror_assignment = DisputeJuror.objects.filter(dispute=dispute, juror=request.user).first()

    if not juror_assignment and not request.user.is_staff:
        messages.error(request, "You are not an assigned juror for this dispute.")
        return redirect('home')

    evaluate_dispute(dispute)
    dispute.refresh_from_db()

    task = dispute.task
    chat_messages = []
    if hasattr(task, 'conversation') and task.conversation:
        raw_messages = task.conversation.messages.all().select_related('sender')
        for msg in raw_messages:
            if msg.sender == task.posted_by:
                display_sender = "Task Poster"
            elif msg.sender == task.taken_by:
                display_sender = "Task Worker"
            else:
                display_sender = "Participant"
            chat_messages.append({
                'sender_label': display_sender,
                'content': msg.content,
                'timestamp': msg.timestamp
            })

    existing_vote = DisputeVote.objects.filter(dispute=dispute, juror=request.user).first()

    context = {
        'dispute': dispute,
        'task': task,
        'chat_messages': chat_messages,
        'existing_vote': existing_vote,
        'has_voted': existing_vote is not None,
    }
    return render(request, 'dispute_adjudicate.html', context)


@login_required(login_url='/login/')
@require_POST
def submit_jury_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    juror_assignment = DisputeJuror.objects.filter(dispute=dispute, juror=request.user).first()

    if not juror_assignment:
        messages.error(request, "You are not authorized to vote on this dispute.")
        return redirect('home')

    evaluate_dispute(dispute)
    dispute.refresh_from_db()

    if dispute.status != 'open':
        messages.error(request, "This dispute voting window is closed or already resolved.")
        return redirect('dispute_adjudicate', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, juror=request.user).exists():
        messages.warning(request, "You have already cast your vote for this dispute.")
        return redirect('dispute_adjudicate', dispute_id=dispute.id)

    vote_choice = request.POST.get('vote')
    if vote_choice not in ['poster_wins', 'worker_wins']:
        messages.error(request, "Invalid vote choice. Please select Poster Wins or Worker Wins.")
        return redirect('dispute_adjudicate', dispute_id=dispute.id)

    DisputeVote.objects.create(
        dispute=dispute,
        juror=request.user,
        vote=vote_choice
    )

    juror_assignment.has_voted = True
    juror_assignment.save()

    evaluate_dispute(dispute)

    messages.success(request, "Your confidential vote has been recorded successfully. Thank you for your service!")
    return redirect('dispute_adjudicate', dispute_id=dispute.id)


@login_required(login_url='/login/')
def my_jury_cases_view(request):
    assignments = DisputeJuror.objects.filter(juror=request.user).select_related('dispute', 'dispute__task').order_by('-assigned_at')
    context = {'assignments': assignments}
    return render(request, 'my_jury_cases.html', context)
