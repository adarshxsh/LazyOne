from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.conf import settings
from django.utils import timezone
from datetime import timedelta
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.contrib.auth.models import User
from ..models import Dispute, Task, Notification, RewardLedger, DisputeAppeal, JuryVote

def check_appeal_timeout(appeal):
    """
    Checks if a dispute appeal has exceeded its 72-hour voting deadline.
    If expired without reaching quorum, refunds the appeal bond and escalates to admin review.
    """
    if appeal.status == 'voting' and appeal.voting_deadline and timezone.now() >= appeal.voting_deadline:
        with transaction.atomic():
            # Check if quorum was somehow met before processing expiration
            assigned_count = appeal.jurors.count()
            majority_threshold = (assigned_count // 2) + 1 if assigned_count > 0 else 1
            poster_votes = appeal.votes.filter(voted_for=appeal.dispute.task.posted_by).count()
            taker_votes = appeal.votes.filter(voted_for=appeal.dispute.task.taken_by).count()
            
            if poster_votes >= majority_threshold:
                finalize_appeal(appeal, appeal.dispute.task.posted_by)
                return
            elif taker_votes >= majority_threshold:
                finalize_appeal(appeal, appeal.dispute.task.taken_by)
                return

            # Expiry reached without quorum: refund appeal bond and escalate to admin
            appeal.status = 'escalated'
            appeal.save()
            
            appellant_profile = appeal.appellant.userprofile
            appellant_profile.rewards += appeal.bond_amount
            appellant_profile.save()
            
            RewardLedger.objects.create(
                user=appeal.appellant,
                task=appeal.dispute.task,
                amount=appeal.bond_amount,
                transaction_type='appeal_bond',
                description=f"Appeal bond refunded due to jury voting timeout for task: '{appeal.dispute.task.title}'"
            )
            
            dispute_link = reverse('dispute_detail', args=[appeal.dispute.id])
            Notification.objects.create(
                recipient=appeal.dispute.task.posted_by,
                message=f"Jury voting period expired without reaching quorum for '{appeal.dispute.task.title}'. Appeal bond refunded and dispute escalated to admin review.",
                link=dispute_link
            )
            if appeal.dispute.task.taken_by:
                Notification.objects.create(
                    recipient=appeal.dispute.task.taken_by,
                    message=f"Jury voting period expired without reaching quorum for '{appeal.dispute.task.title}'. Appeal bond refunded and dispute escalated to admin review.",
                    link=dispute_link
                )


def finalize_appeal(appeal, winner_user):
    """
    Finalizes an appeal when a majority voting quorum is reached:
    1. Updates dispute and appeal statuses to resolved.
    2. Executes task reward payout or cancellation refund to the winner.
    3. Handles bond refund to winning appellant OR slashing penalty for losing party.
    4. Distributes slash pool to majority jurors as governance rewards.
    5. Sends final notifications to litigants and jurors.
    """
    dispute = appeal.dispute
    task = dispute.task
    posted_by = task.posted_by
    taken_by = task.taken_by
    loser_user = taken_by if winner_user == posted_by else posted_by
    appellant = appeal.appellant

    with transaction.atomic():
        appeal.status = 'resolved'
        appeal.winner = winner_user
        appeal.save()

        dispute.status = 'resolved'
        dispute.save()

        # 1. Task Reward Settlement
        if winner_user == taken_by:
            task.status = 'completed'
            task.save()
            taken_by_profile = taken_by.userprofile
            taken_by_profile.rewards += task.reward
            taken_by_profile.save()
            RewardLedger.objects.create(
                user=taken_by,
                task=task,
                amount=task.reward,
                transaction_type='task_completion',
                description=f"Task reward awarded for winning dispute appeal on task: '{task.title}'"
            )
        else:
            task.status = 'cancelled'
            task.save()
            posted_by_profile = posted_by.userprofile
            posted_by_profile.rewards += task.reward
            posted_by_profile.save()
            RewardLedger.objects.create(
                user=posted_by,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Task reward refunded for winning dispute appeal on task: '{task.title}'"
            )

        # 2. Appeal Bond & Slashing Settlement
        if winner_user == appellant:
            # Appellant won: refund appellant's bond
            appellant_profile = appellant.userprofile
            appellant_profile.rewards += appeal.bond_amount
            appellant_profile.save()
            RewardLedger.objects.create(
                user=appellant,
                task=task,
                amount=appeal.bond_amount,
                transaction_type='appeal_bond',
                description=f"Appeal bond refunded for winning dispute on task: '{task.title}'"
            )
            # Slash losing respondent (capped at available reward balance to prevent negative balance)
            respondent = loser_user
            respondent_profile = respondent.userprofile
            slash_amount = min(respondent_profile.rewards, appeal.bond_amount)
            if slash_amount > 0:
                respondent_profile.rewards -= slash_amount
                respondent_profile.save()
                RewardLedger.objects.create(
                    user=respondent,
                    task=task,
                    amount=-slash_amount,
                    transaction_type='slashing_penalty',
                    description=f"Slashing penalty for losing dispute appeal on task: '{task.title}'"
                )
            slash_pool = slash_amount
        else:
            # Appellant lost: appellant's deposited bond is slashed
            RewardLedger.objects.create(
                user=appellant,
                task=task,
                amount=appeal.bond_amount,
                transaction_type='appeal_bond',
                description=f"Appeal bond reservation released for slashing on task: '{task.title}'"
            )
            RewardLedger.objects.create(
                user=appellant,
                task=task,
                amount=-appeal.bond_amount,
                transaction_type='slashing_penalty',
                description=f"Appeal bond slashed for losing dispute appeal on task: '{task.title}'"
            )
            slash_pool = appeal.bond_amount

        # 3. Governance Reward Distribution to Majority Jurors
        majority_votes = appeal.votes.filter(voted_for=winner_user)
        num_majority = majority_votes.count()
        if num_majority > 0 and slash_pool > 0:
            reward_per_juror = slash_pool // num_majority
            if reward_per_juror > 0:
                for vote in majority_votes:
                    juror_profile = vote.juror.userprofile
                    juror_profile.rewards += reward_per_juror
                    juror_profile.save()
                    RewardLedger.objects.create(
                        user=vote.juror,
                        task=task,
                        amount=reward_per_juror,
                        transaction_type='juror_reward',
                        description=f"Governance reward for majority jury vote on task: '{task.title}'"
                    )

        # 4. Notifications
        dispute_link = reverse('dispute_detail', args=[dispute.id])
        Notification.objects.create(
            recipient=winner_user,
            message=f"The peer jury ruled in your favor for task '{task.title}'. Rewards have been distributed.",
            link=dispute_link
        )
        Notification.objects.create(
            recipient=loser_user,
            message=f"The peer jury ruled against you for task '{task.title}'.",
            link=dispute_link
        )
        rewards_link = reverse('rewards')
        for vote in majority_votes:
            Notification.objects.create(
                recipient=vote.juror,
                message=f"You received governance reward points for voting with the majority in dispute for '{task.title}'.",
                link=rewards_link
            )


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    appeal = getattr(dispute, 'appeal', None)

    if appeal:
        check_appeal_timeout(appeal)

    is_litigant = (request.user == task.posted_by or request.user == task.taken_by)
    is_juror = appeal and (request.user in appeal.jurors.all())

    if not is_litigant and not is_juror and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    user_vote = None
    if appeal and is_juror:
        user_vote = JuryVote.objects.filter(appeal=appeal, juror=request.user).first()

    assigned_jurors_count = appeal.jurors.count() if appeal else 0
    votes_count = appeal.votes.count() if appeal else 0
    majority_threshold = (assigned_jurors_count // 2) + 1 if assigned_jurors_count > 0 else 1

    appeal_bond_amount = getattr(settings, 'JURY_APPEAL_BOND', 100)

    context = {
        'dispute': dispute,
        'task': task,
        'appeal': appeal,
        'is_litigant': is_litigant,
        'is_juror': is_juror,
        'user_vote': user_vote,
        'assigned_jurors_count': assigned_jurors_count,
        'votes_count': votes_count,
        'majority_threshold': majority_threshold,
        'appeal_bond_amount': appeal_bond_amount,
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
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    deposit_amount=deposit_amount,
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

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"{request.user.username} has raised a dispute for your task: '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond.")
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
        if hasattr(dispute, 'appeal'):
            appeal = dispute.appeal
            if appeal.status == 'voting':
                appellant_profile = appeal.appellant.userprofile
                appellant_profile.rewards += appeal.bond_amount
                appellant_profile.save()
                RewardLedger.objects.create(
                    user=appeal.appellant,
                    task=task,
                    amount=appeal.bond_amount,
                    transaction_type='appeal_bond',
                    description=f"Appeal bond refunded upon dispute withdrawal for task: '{task.title}'"
                )

        dispute.status = 'resolved'
        dispute.save()

        task.status = 'in_progress'
        task.save()

        recipient = task.posted_by if request.user == task.taken_by else task.taken_by
        if recipient:
            Notification.objects.create(
                recipient=recipient,
                message=f"{request.user.username} has withdrawn the dispute for '{task.title}'. The task is now in progress.",
                link=reverse('my_tasks')
            )
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')


@login_required(login_url='/login/')
@require_POST
def file_appeal(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "Only task litigants can file an appeal.")
        return redirect('home')

    if dispute.status != 'open':
        messages.error(request, "Only open disputes can be appealed.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if hasattr(dispute, 'appeal'):
        messages.info(request, "An appeal has already been filed for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    bond_amount = getattr(settings, 'JURY_APPEAL_BOND', 100)
    user_profile = request.user.userprofile

    if user_profile.rewards < bond_amount:
        messages.error(request, f"You need at least {bond_amount} points to post an appeal bond. Your current balance is {user_profile.rewards}.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        # Reserve bond atomically
        user_profile.rewards -= bond_amount
        user_profile.save()

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-bond_amount,
            transaction_type='appeal_bond',
            description=f"Appeal bond reserved for task: '{task.title}'"
        )

        dispute.status = 'appealed'
        dispute.save()

        # Juror selection & neutrality enforcement
        litigant_ids = {task.posted_by.id}
        if task.taken_by:
            litigant_ids.add(task.taken_by.id)

        poster_friends = set(task.posted_by.userprofile.friends.all().values_list('user_id', flat=True))
        taker_friends = set(task.taken_by.userprofile.friends.all().values_list('user_id', flat=True)) if task.taken_by else set()

        excluded_ids = litigant_ids | poster_friends | taker_friends

        panel_size = getattr(settings, 'JURY_PANEL_SIZE', 3)
        eligible_jurors = list(
            User.objects.filter(is_active=True, userprofile__voting_suspended=False)
            .exclude(id__in=excluded_ids)
            .order_by('?')[:panel_size]
        )

        deadline = timezone.now() + timedelta(hours=72)
        appeal = DisputeAppeal.objects.create(
            dispute=dispute,
            appellant=request.user,
            bond_amount=bond_amount,
            voting_deadline=deadline
        )
        if eligible_jurors:
            appeal.jurors.set(eligible_jurors)

        dispute_link = reverse('dispute_detail', args=[dispute.id])
        opposing_litigant = task.taken_by if request.user == task.posted_by else task.posted_by
        if opposing_litigant:
            Notification.objects.create(
                recipient=opposing_litigant,
                message=f"{request.user.username} has filed an appeal for dispute on task '{task.title}'. A peer jury panel has been assigned.",
                link=dispute_link
            )

        for juror in eligible_jurors:
            Notification.objects.create(
                recipient=juror,
                message=f"You have been assigned as a peer juror for a dispute on task '{task.title}'. Please cast your vote.",
                link=dispute_link
            )

    messages.success(request, f"Appeal filed successfully! {bond_amount} points reserved as appeal bond.")
    return redirect('dispute_detail', dispute_id=dispute.id)


@login_required(login_url='/login/')
@require_POST
def cast_jury_vote(request, appeal_id):
    appeal = get_object_or_404(DisputeAppeal, id=appeal_id)
    dispute = appeal.dispute
    task = dispute.task

    check_appeal_timeout(appeal)

    if appeal.status != 'voting':
        messages.error(request, "Voting is closed for this appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.user not in appeal.jurors.all():
        messages.error(request, "You are not assigned as a juror for this dispute appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if JuryVote.objects.filter(appeal=appeal, juror=request.user).exists():
        messages.info(request, "You have already cast your vote in this appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_id = request.POST.get('voted_for_id')
    if not voted_for_id or int(voted_for_id) not in [task.posted_by.id, task.taken_by.id if task.taken_by else None]:
        messages.error(request, "Invalid vote selection.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_user = get_object_or_404(User, id=voted_for_id)

    with transaction.atomic():
        JuryVote.objects.create(
            appeal=appeal,
            juror=request.user,
            voted_for=voted_for_user
        )

        dispute_link = reverse('dispute_detail', args=[dispute.id])
        for party in [task.posted_by, task.taken_by]:
            if party:
                Notification.objects.create(
                    recipient=party,
                    message=f"A juror has cast a vote in the dispute appeal for '{task.title}'.",
                    link=dispute_link
                )

        assigned_count = appeal.jurors.count()
        if assigned_count > 0:
            majority_threshold = (assigned_count // 2) + 1
            poster_votes = appeal.votes.filter(voted_for=task.posted_by).count()
            taker_votes = appeal.votes.filter(voted_for=task.taken_by).count() if task.taken_by else 0

            if poster_votes >= majority_threshold:
                finalize_appeal(appeal, task.posted_by)
            elif taker_votes >= majority_threshold:
                finalize_appeal(appeal, task.taken_by)

    messages.success(request, "Your vote has been submitted successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)
