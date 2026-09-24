from datetime import timedelta
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.contrib.auth.models import User
from ..models import Dispute, Task, Notification, RewardLedger, DisputeVote


def check_and_apply_default_resolution(dispute):
    """
    Checks if a dispute in 'pending_counter_deposit' has exceeded the 24-hour window
    without the responding party posting a matching counter-deposit bond.
    If expired, resolves the dispute by default in favor of the initiator.
    """
    if dispute.status == 'pending_counter_deposit':
        deadline = dispute.created_at + timedelta(hours=24)
        if timezone.now() >= deadline:
            with transaction.atomic():
                task = dispute.task
                initiator = dispute.raised_by

                # 1. Refund initiator's deposit bond
                initiator_profile = initiator.userprofile
                initiator_profile.rewards += dispute.initiator_deposit_amount
                initiator_profile.save()

                RewardLedger.objects.create(
                    user=initiator,
                    task=task,
                    amount=dispute.initiator_deposit_amount,
                    transaction_type='dispute_refund',
                    description=f"Initiator deposit bond refunded by default dispute resolution on task: '{task.title}'"
                )
                dispute.escrow_status = 'refunded'

                # 2. Award task reward to initiator & update task status
                if initiator == task.taken_by:
                    # Worker won by default -> complete task & award task reward
                    initiator_profile.rewards += task.reward
                    initiator_profile.save()
                    RewardLedger.objects.create(
                        user=initiator,
                        task=task,
                        amount=task.reward,
                        transaction_type='task_completion',
                        description=f"Task reward awarded by default dispute resolution on task: '{task.title}'"
                    )
                    task.status = 'completed'
                else:
                    # Poster won by default -> cancel task & refund task reward
                    initiator_profile.rewards += task.reward
                    initiator_profile.save()
                    RewardLedger.objects.create(
                        user=initiator,
                        task=task,
                        amount=task.reward,
                        transaction_type='task_cancellation',
                        description=f"Task reward refunded by default dispute resolution on task: '{task.title}'"
                    )
                    task.status = 'cancelled'

                dispute.status = 'resolved'
                dispute.save()
                task.save()

                responder = task.posted_by if initiator == task.taken_by else task.taken_by
                Notification.objects.create(
                    recipient=initiator,
                    message=f"Dispute resolved in your favor by default for task: '{task.title}' (responding party failed to post counter-deposit within 24 hours).",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
                Notification.objects.create(
                    recipient=responder,
                    message=f"Dispute for task '{task.title}' was resolved by default in favor of {initiator.username} because counter-deposit was not posted within 24 hours.",
                    link=reverse('dispute_detail', args=[dispute.id])
                )
            return True
    return False


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    check_and_apply_default_resolution(dispute)
    dispute.refresh_from_db()
    task = dispute.task

    responder = task.posted_by if dispute.raised_by == task.taken_by else task.taken_by
    is_responder = (request.user == responder)
    is_initiator = (request.user == dispute.raised_by)
    is_dispute_participant = (request.user == task.posted_by or request.user == task.taken_by)

    has_voted = DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists() if request.user.is_authenticated else False
    user_vote = DisputeVote.objects.filter(dispute=dispute, voter=request.user).first() if has_voted else None

    poster_votes_count = dispute.votes.filter(voted_for=task.posted_by).count()
    worker_votes_count = dispute.votes.filter(voted_for=task.taken_by).count()
    total_votes_count = dispute.votes.count()

    counter_deposit_deadline = dispute.created_at + timedelta(hours=24)
    time_remaining_seconds = max(0, int((counter_deposit_deadline - timezone.now()).total_seconds())) if dispute.status == 'pending_counter_deposit' else 0

    context = {
        'dispute': dispute,
        'task': task,
        'responder': responder,
        'is_responder': is_responder,
        'is_initiator': is_initiator,
        'is_dispute_participant': is_dispute_participant,
        'has_voted': has_voted,
        'user_vote': user_vote,
        'poster_votes_count': poster_votes_count,
        'worker_votes_count': worker_votes_count,
        'total_votes_count': total_votes_count,
        'counter_deposit_deadline': counter_deposit_deadline,
        'time_remaining_seconds': time_remaining_seconds,
    }
    return render(request, 'dispute_detail.html', context)


@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status in ['pending_counter_deposit', 'active_voting', 'open']:
        check_and_apply_default_resolution(task.dispute)
        return redirect('dispute_detail', dispute_id=task.dispute.id)

    if (task.taken_by != request.user and task.posted_by != request.user) or task.status != 'in_progress':
        messages.error(request, "You can only raise a dispute for an active task you are participating in.")
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

        with transaction.atomic():
            user_profile.rewards -= deposit_amount
            user_profile.save()

            now = timezone.now()
            if hasattr(task, 'dispute'):
                dispute = task.dispute
                dispute.raised_by = request.user
                dispute.reason = reason
                dispute.status = 'pending_counter_deposit'
                dispute.deposit_amount = deposit_amount
                dispute.initiator_deposit_amount = deposit_amount
                dispute.counter_deposit_amount = 0
                dispute.escrow_status = 'held'
                dispute.created_at = now
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    status='pending_counter_deposit',
                    deposit_amount=deposit_amount,
                    initiator_deposit_amount=deposit_amount,
                    counter_deposit_amount=0,
                    escrow_status='held'
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

            responding_party = task.posted_by if request.user == task.taken_by else task.taken_by
            Notification.objects.create(
                recipient=responding_party,
                message=f"{request.user.username} has raised a dispute for task: '{task.title}'. A matching counter-deposit bond of {deposit_amount} points is required within 24 hours.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond. Responding party has 24 hours to post a matching counter-deposit.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    return redirect('my_tasks')


@login_required(login_url='/login/')
@require_POST
def post_counter_deposit(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    if check_and_apply_default_resolution(dispute):
        messages.info(request, "The 24-hour counter-deposit window has passed; dispute was resolved by default.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status != 'pending_counter_deposit':
        messages.error(request, "This dispute is not awaiting a counter-deposit.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    task = dispute.task
    responding_party = task.posted_by if dispute.raised_by == task.taken_by else task.taken_by
    if request.user != responding_party:
        messages.error(request, "Only the responding party can post the matching counter-deposit bond.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    deposit_amount = task.deposit_bond_amount
    user_profile = request.user.userprofile
    if user_profile.rewards < deposit_amount:
        messages.error(
            request,
            f"Insufficient reward points balance. You need at least {deposit_amount} points to post a matching counter-deposit bond, but you only have {user_profile.rewards} points."
        )
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        user_profile.rewards -= deposit_amount
        user_profile.save()

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-deposit_amount,
            transaction_type='counter_dispute_deposit',
            description=f"Matching counter-deposit bond held for dispute on task: '{task.title}'"
        )

        dispute.counter_deposit_amount = deposit_amount
        dispute.deposit_amount = dispute.initiator_deposit_amount + deposit_amount
        dispute.status = 'active_voting'
        dispute.save()

        Notification.objects.create(
            recipient=dispute.raised_by,
            message=f"{request.user.username} has posted a matching counter-deposit for dispute on '{task.title}'. Dispute is now open for community juror voting.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, f"Matching counter-deposit of {deposit_amount} points posted successfully. Dispute is now in active voting.")
    return redirect('dispute_detail', dispute_id=dispute.id)


@login_required(login_url='/login/')
@require_POST
def cast_dispute_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    if check_and_apply_default_resolution(dispute):
        messages.info(request, "Dispute voting has ended as the counter-deposit deadline expired.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status != 'active_voting':
        messages.error(request, "This dispute is not currently in active voting status.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    task = dispute.task
    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "Task poster and worker are not permitted to vote on their own dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
        messages.error(request, "You have already cast a juror vote on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_id = request.POST.get('voted_for')
    if not voted_for_id:
        messages.error(request, "Please select a party to vote for.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for = get_object_or_404(User, id=voted_for_id)
    if voted_for not in [task.posted_by, task.taken_by]:
        messages.error(request, "Invalid vote target.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    user_profile = request.user.userprofile
    stake_amount = 50
    if user_profile.rewards < stake_amount:
        messages.error(
            request,
            f"Insufficient reward points balance. You need at least {stake_amount} points as a commitment stake to vote, but you have {user_profile.rewards} points."
        )
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        user_profile.rewards -= stake_amount
        user_profile.save()

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-stake_amount,
            transaction_type='juror_stake',
            description=f"Juror vote stake committed for dispute on task: '{task.title}'"
        )

        DisputeVote.objects.create(
            dispute=dispute,
            voter=request.user,
            voted_for=voted_for,
            stake_amount=stake_amount
        )

    messages.success(request, f"Your juror vote for {voted_for.username} has been recorded with a {stake_amount}-point commitment stake.")
    return redirect('dispute_detail', dispute_id=dispute.id)


@login_required(login_url='/login/')
def resolve_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    if check_and_apply_default_resolution(dispute):
        messages.info(request, "Dispute was resolved by default.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status == 'resolved':
        messages.info(request, "This dispute is already resolved.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status != 'active_voting':
        messages.error(request, "Dispute cannot be resolved at this stage.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    task = dispute.task

    with transaction.atomic():
        poster_votes = dispute.votes.filter(voted_for=task.posted_by).count()
        worker_votes = dispute.votes.filter(voted_for=task.taken_by).count()

        if worker_votes > poster_votes:
            winning_party = task.taken_by
            losing_party = task.posted_by
        elif poster_votes > worker_votes:
            winning_party = task.posted_by
            losing_party = task.taken_by
        else:
            # Tie or 0 votes -> default to dispute initiator
            winning_party = dispute.raised_by
            losing_party = task.posted_by if dispute.raised_by == task.taken_by else task.taken_by

        # 1. Refund winning party's deposit bond
        winning_deposit = dispute.initiator_deposit_amount if winning_party == dispute.raised_by else dispute.counter_deposit_amount
        winner_profile = winning_party.userprofile
        winner_profile.rewards += winning_deposit
        winner_profile.save()

        RewardLedger.objects.create(
            user=winning_party,
            task=task,
            amount=winning_deposit,
            transaction_type='dispute_refund',
            description=f"Security deposit bond refunded for winning dispute on task: '{task.title}'"
        )

        # 2. Award losing party's forfeited deposit bond to winning party
        losing_deposit = dispute.counter_deposit_amount if losing_party != dispute.raised_by else dispute.initiator_deposit_amount
        winner_profile.rewards += losing_deposit
        winner_profile.save()

        RewardLedger.objects.create(
            user=winning_party,
            task=task,
            amount=losing_deposit,
            transaction_type='dispute_forfeit',
            description=f"Forfeited dispute deposit bond awarded from task: '{task.title}'"
        )

        RewardLedger.objects.create(
            user=losing_party,
            task=task,
            amount=0,
            transaction_type='dispute_forfeit',
            description=f"Security deposit bond forfeited for losing dispute on task: '{task.title}'"
        )

        # 3. Handle task reward points & task status
        if winning_party == task.taken_by:
            # Worker wins -> complete task & award task reward
            winner_profile.rewards += task.reward
            winner_profile.save()
            RewardLedger.objects.create(
                user=winning_party,
                task=task,
                amount=task.reward,
                transaction_type='task_completion',
                description=f"Task reward awarded via dispute resolution for task: '{task.title}'"
            )
            task.status = 'completed'
        else:
            # Poster wins -> cancel task & refund task reward
            winner_profile.rewards += task.reward
            winner_profile.save()
            RewardLedger.objects.create(
                user=winning_party,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Task reward refunded via dispute resolution for task: '{task.title}'"
            )
            task.status = 'cancelled'

        # 4. Handle Juror Stake Slashing and Reward Pool Payouts
        all_votes = list(dispute.votes.all())
        majority_votes = [v for v in all_votes if v.voted_for == winning_party]
        minority_votes = [v for v in all_votes if v.voted_for != winning_party]

        slashed_pool = sum(v.stake_amount for v in minority_votes)

        for v in minority_votes:
            RewardLedger.objects.create(
                user=v.voter,
                task=task,
                amount=0,
                transaction_type='juror_slash',
                description=f"Juror stake slashed for minority vote on dispute: '{task.title}'"
            )

        if majority_votes:
            num_majority = len(majority_votes)
            base_share = slashed_pool // num_majority
            remainder = slashed_pool % num_majority

            for idx, v in enumerate(majority_votes):
                extra = 1 if idx < remainder else 0
                share = base_share + extra
                payout = v.stake_amount + share

                voter_profile = v.voter.userprofile
                voter_profile.rewards += payout
                voter_profile.save()

                RewardLedger.objects.create(
                    user=v.voter,
                    task=task,
                    amount=payout,
                    transaction_type='juror_reward',
                    description=f"Juror reward payout (stake refunded + reward share) for winning vote on dispute: '{task.title}'"
                )

        dispute.escrow_status = 'forfeited'
        dispute.status = 'resolved'
        dispute.save()
        task.save()

        Notification.objects.create(
            recipient=winning_party,
            message=f"Dispute resolved in your favor for task: '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )
        Notification.objects.create(
            recipient=losing_party,
            message=f"Dispute resolved in favor of {winning_party.username} for task: '{task.title}'.",
            link=reverse('dispute_detail', args=[dispute.id])
        )

    messages.success(request, f"Dispute resolved in favor of {winning_party.username}. Rewards and juror stakes processed.")
    return redirect('dispute_detail', dispute_id=dispute.id)


@login_required(login_url='/login/')
@require_POST
def withdraw_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id, raised_by=request.user)
    if dispute.status == 'resolved':
        messages.info(request, "This dispute is already resolved.")
        return redirect('my_tasks')

    task = dispute.task
    with transaction.atomic():
        # 1. Refund initiator's deposit
        initiator_profile = request.user.userprofile
        initiator_profile.rewards += dispute.initiator_deposit_amount
        initiator_profile.save()

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=dispute.initiator_deposit_amount,
            transaction_type='dispute_refund',
            description=f"Security deposit bond refunded for withdrawn dispute on task: '{task.title}'"
        )

        # 2. Refund counter-deposit if posted
        if dispute.counter_deposit_amount > 0:
            responder = task.posted_by if dispute.raised_by == task.taken_by else task.taken_by
            responder_profile = responder.userprofile
            responder_profile.rewards += dispute.counter_deposit_amount
            responder_profile.save()

            RewardLedger.objects.create(
                user=responder,
                task=task,
                amount=dispute.counter_deposit_amount,
                transaction_type='dispute_refund',
                description=f"Counter-deposit bond refunded for withdrawn dispute on task: '{task.title}'"
            )

        # 3. Refund juror stakes if any
        for v in dispute.votes.all():
            voter_profile = v.voter.userprofile
            voter_profile.rewards += v.stake_amount
            voter_profile.save()

            RewardLedger.objects.create(
                user=v.voter,
                task=task,
                amount=v.stake_amount,
                transaction_type='dispute_refund',
                description=f"Juror stake refunded due to withdrawn dispute on task: '{task.title}'"
            )

        dispute.escrow_status = 'refunded'
        dispute.status = 'resolved'
        dispute.save()

        task.status = 'in_progress'
        task.save()

        responder = task.posted_by if dispute.raised_by == task.taken_by else task.taken_by
        Notification.objects.create(
            recipient=responder,
            message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
            link=reverse('my_tasks')
        )

    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')
