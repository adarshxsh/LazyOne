import math
import random
from datetime import timedelta
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.utils import timezone
from django.db import transaction
from django.db.models import Q
from django.contrib.auth.models import User
from ..models import (
    Dispute, Task, Notification, RewardLedger,
    JuryAssignment, DisputeVote, DisputeEvidence,
    Friendship, FriendRequest
)


def sample_jurors(dispute):
    task = dispute.task
    excluded_ids = {task.posted_by.id}
    if task.taken_by:
        excluded_ids.add(task.taken_by.id)

    for party in [task.posted_by, task.taken_by]:
        if not party:
            continue
        if hasattr(party, 'userprofile'):
            p_prof = party.userprofile
            for friend_prof in p_prof.friends.all():
                excluded_ids.add(friend_prof.user.id)
            for fs in Friendship.objects.filter(from_user=p_prof):
                excluded_ids.add(fs.to_user.user.id)
            for fs in Friendship.objects.filter(to_user=p_prof):
                excluded_ids.add(fs.from_user.user.id)
        for fr in FriendRequest.objects.filter(from_user=party, is_accepted=True):
            excluded_ids.add(fr.to_user.id)
        for fr in FriendRequest.objects.filter(to_user=party, is_accepted=True):
            excluded_ids.add(fr.from_user.id)

    candidates = User.objects.filter(
        is_active=True,
        userprofile__rewards__gte=50
    ).filter(
        Q(userprofile__is_phone_verified=True) | Q(userprofile__is_instagram_verified=True)
    ).exclude(id__in=excluded_ids)

    cand_list = list(candidates)
    if len(cand_list) < 5:
        # Fallback if verified user count is under 5
        fallback = list(User.objects.filter(is_active=True, userprofile__rewards__gte=50).exclude(id__in=excluded_ids))
        if len(fallback) > len(cand_list):
            cand_list = fallback

    sampled = random.sample(cand_list, min(5, len(cand_list)))

    JuryAssignment.objects.filter(dispute=dispute).delete()

    assignments = []
    for juror in sampled:
        assignment = JuryAssignment.objects.create(dispute=dispute, juror=juror)
        assignments.append(assignment)
        Notification.objects.create(
            recipient=juror,
            message=f"You have been randomly selected as a neutral juror for dispute on task '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
    return sampled


def check_dispute_expiration(dispute):
    if dispute.status not in ['VOTING_ACTIVE', 'JURY_SELECTION', 'EVIDENCE_SUBMISSION']:
        return
    now = timezone.now()
    if dispute.voting_deadline and now > dispute.voting_deadline:
        poster_votes = dispute.votes.filter(choice='poster').count()
        taker_votes = dispute.votes.filter(choice='taker').count()
        split_votes = dispute.votes.filter(choice='split').count()

        if poster_votes >= 3 or taker_votes >= 3 or split_votes >= 3:
            check_and_settle_dispute(dispute)
            return

        with transaction.atomic():
            dispute.status = 'EXPIRED_FALLBACK'
            dispute.save()

            for vote in dispute.votes.all():
                profile = vote.juror.userprofile
                profile.rewards += vote.staked_amount
                profile.save()
                RewardLedger.objects.create(
                    user=vote.juror,
                    task=dispute.task,
                    amount=vote.staked_amount,
                    transaction_type='juror_reward',
                    description=f"Juror stake refund (Dispute Expired) for task '{dispute.task.title}'"
                )

            Notification.objects.create(
                recipient=dispute.task.posted_by,
                message=f"Dispute for task '{dispute.task.title}' expired without jury consensus and is queued for staff review.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
            if dispute.task.taken_by:
                Notification.objects.create(
                    recipient=dispute.task.taken_by,
                    message=f"Dispute for task '{dispute.task.title}' expired without jury consensus and is queued for staff review.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )


def check_and_settle_dispute(dispute):
    poster_votes = dispute.votes.filter(choice='poster').count()
    taker_votes = dispute.votes.filter(choice='taker').count()
    split_votes = dispute.votes.filter(choice='split').count()

    winning_choice = None
    if poster_votes >= 3:
        winning_choice = 'poster'
    elif taker_votes >= 3:
        winning_choice = 'taker'
    elif split_votes >= 3:
        winning_choice = 'split'

    if not winning_choice:
        return False

    with transaction.atomic():
        dispute.status = 'CONSENSUS_REACHED'
        dispute.save()

        task = dispute.task
        reward = task.reward

        if winning_choice == 'taker':
            taker_profile = task.taken_by.userprofile
            taker_profile.rewards += reward
            taker_profile.save()
            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=reward,
                transaction_type='dispute_payout',
                description=f"Dispute escrow payout for task '{task.title}'"
            )
            task.status = 'completed'
        elif winning_choice == 'poster':
            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += reward
            poster_profile.save()
            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=reward,
                transaction_type='dispute_refund',
                description=f"Dispute escrow refund for task '{task.title}'"
            )
            task.status = 'cancelled'
        elif winning_choice == 'split':
            taker_share = math.floor(reward * 50 / 100)
            poster_share = reward - taker_share

            taker_profile = task.taken_by.userprofile
            taker_profile.rewards += taker_share
            taker_profile.save()
            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=taker_share,
                transaction_type='dispute_split',
                description=f"Dispute 50% split payout for task '{task.title}'"
            )

            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += poster_share
            poster_profile.save()
            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=poster_share,
                transaction_type='dispute_split',
                description=f"Dispute 50% split refund for task '{task.title}'"
            )
            task.status = 'completed'

        task.save()

        winning_votes = list(dispute.votes.filter(choice=winning_choice))
        losing_votes = list(dispute.votes.exclude(choice=winning_choice))

        total_losing_stakes = sum(v.staked_amount for v in losing_votes)
        N_win = len(winning_votes)

        if N_win > 0:
            bonus_per_winner = total_losing_stakes // N_win
            remainder = total_losing_stakes % N_win

            for idx, vote in enumerate(winning_votes):
                extra = 1 if idx < remainder else 0
                payout = vote.staked_amount + bonus_per_winner + extra
                juror_profile = vote.juror.userprofile
                juror_profile.rewards += payout
                juror_profile.save()

                RewardLedger.objects.create(
                    user=vote.juror,
                    task=task,
                    amount=payout,
                    transaction_type='juror_reward',
                    description=f"Juror stake return & bonus for dispute on task '{task.title}'"
                )

        dispute.refund_deposit()
        dispute.status = 'RESOLVED'
        dispute.resolved_at = timezone.now()
        dispute.save()

        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Dispute for task '{task.title}' resolved in favor of {winning_choice.upper()}.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Dispute for task '{task.title}' resolved in favor of {winning_choice.upper()}.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        for vote in winning_votes:
            Notification.objects.create(
                recipient=vote.juror,
                message=f"You earned jury rewards for dispute on task '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    return True


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    check_dispute_expiration(dispute)
    task = dispute.task

    is_juror = dispute.jury_assignments.filter(juror=request.user).exists()
    is_participant = request.user in [task.posted_by, task.taken_by]
    is_staff = request.user.is_staff

    if not (is_participant or is_juror or is_staff):
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    user_vote = dispute.votes.filter(juror=request.user).first()
    can_vote = is_juror and not user_vote and dispute.status == 'VOTING_ACTIVE'

    poster_votes = dispute.votes.filter(choice='poster').count()
    taker_votes = dispute.votes.filter(choice='taker').count()
    split_votes = dispute.votes.filter(choice='split').count()
    total_votes = dispute.votes.count()

    evidences = dispute.evidence_entries.all().order_by('submitted_at')

    context = {
        'dispute': dispute,
        'task': task,
        'is_juror': is_juror,
        'is_participant': is_participant,
        'is_staff': is_staff,
        'user_vote': user_vote,
        'can_vote': can_vote,
        'poster_votes': poster_votes,
        'taker_votes': taker_votes,
        'split_votes': split_votes,
        'total_votes': total_votes,
        'evidences': evidences,
    }
    return render(request, 'dispute_detail.html', context)


@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status in ['open', 'VOTING_ACTIVE', 'EVIDENCE_SUBMISSION', 'JURY_SELECTION']:
        return redirect('dispute_detail', dispute_id=task.dispute.id)
    if request.user not in [task.posted_by, task.taken_by] or task.status != 'in_progress':
        messages.error(request, "You can only raise a dispute for a task you are part of that is currently in progress.")
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

        now = timezone.now()
        with transaction.atomic():
            user_profile.rewards -= deposit_amount
            user_profile.save()

            if hasattr(task, 'dispute'):
                dispute = task.dispute
                dispute.raised_by = request.user
                dispute.reason = reason
                dispute.status = 'EVIDENCE_SUBMISSION'
                dispute.deposit_amount = deposit_amount
                dispute.escrow_status = 'held'
                dispute.created_at = now
                dispute.evidence_deadline = now + timedelta(hours=24)
                dispute.voting_deadline = now + timedelta(hours=48)
                dispute.resolved_at = None
                dispute.save()
                dispute.votes.all().delete()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    deposit_amount=deposit_amount,
                    escrow_status='held',
                    status='EVIDENCE_SUBMISSION',
                    evidence_deadline=now + timedelta(hours=24),
                    voting_deadline=now + timedelta(hours=48)
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

            dispute.status = 'JURY_SELECTION'
            dispute.save()
            sample_jurors(dispute)
            dispute.status = 'VOTING_ACTIVE'
            dispute.save()

            counterparty = task.taken_by if request.user == task.posted_by else task.posted_by
            if counterparty:
                Notification.objects.create(
                    recipient=counterparty,
                    message=f"{request.user.username} has raised a dispute for task: '{task.title}'.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )

        messages.success(request, f"Dispute raised successfully and sent to jury voting. {deposit_amount} points held as deposit bond.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    return redirect('my_tasks')


@login_required(login_url='/login/')
@require_POST
def submit_dispute_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    check_dispute_expiration(dispute)

    if dispute.status != 'VOTING_ACTIVE':
        messages.error(request, "Voting is not active for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not dispute.jury_assignments.filter(juror=request.user).exists():
        messages.error(request, "You are not an assigned juror for this dispute.")
        return redirect('home')

    if dispute.votes.filter(juror=request.user).exists():
        messages.error(request, "You have already voted on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    choice = request.POST.get('choice', '').lower()
    if choice not in ['poster', 'taker', 'split']:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    user_profile = request.user.userprofile
    if user_profile.rewards < 10:
        messages.error(request, "You need at least 10 reward points stake to vote.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        user_profile.rewards -= 10
        user_profile.save()

        RewardLedger.objects.create(
            user=request.user,
            task=dispute.task,
            amount=-10,
            transaction_type='juror_stake',
            description=f"Juror stake for dispute on task '{dispute.task.title}'"
        )

        vote = DisputeVote.objects.create(
            dispute=dispute,
            juror=request.user,
            choice=choice,
            staked_amount=10
        )

        JuryAssignment.objects.filter(dispute=dispute, juror=request.user).update(has_staked=True)

        check_and_settle_dispute(dispute)

    messages.success(request, "Your vote and 10-point stake have been submitted successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)


@login_required(login_url='/login/')
@require_POST
def submit_dispute_evidence(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    is_juror = dispute.jury_assignments.filter(juror=request.user).exists()
    is_participant = request.user in [task.posted_by, task.taken_by]

    if not (is_participant or is_juror or request.user.is_staff):
        messages.error(request, "You are not authorized to submit evidence for this dispute.")
        return redirect('home')

    text = request.POST.get('text', '')
    evidence_file = request.FILES.get('file')

    if not text and not evidence_file:
        messages.error(request, "Please provide text or attach a file as evidence.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    DisputeEvidence.objects.create(
        dispute=dispute,
        submitted_by=request.user,
        text=text,
        file=evidence_file
    )
    messages.success(request, "Evidence submitted successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)


@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    task = dispute.task
    with transaction.atomic():
        dispute.refund_deposit(
            reason_description=f"Security deposit bond refunded for withdrawn dispute on task: '{task.title}'"
        )
        for vote in dispute.votes.all():
            profile = vote.juror.userprofile
            profile.rewards += vote.staked_amount
            profile.save()
            RewardLedger.objects.create(
                user=vote.juror,
                task=task,
                amount=vote.staked_amount,
                transaction_type='juror_reward',
                description=f"Juror stake refund for withdrawn dispute on task '{task.title}'"
            )

        task.status = 'in_progress'
        task.save()
        dispute.status = 'withdrawn'
        dispute.save()

        counterparty = task.taken_by if request.user == task.posted_by else task.posted_by
        if counterparty:
            Notification.objects.create(
                recipient=counterparty,
                message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
                link=reverse('my_tasks')
            )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')
