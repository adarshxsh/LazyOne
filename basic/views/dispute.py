from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from django.contrib.auth.models import User
from django.views.decorators.http import require_POST
from django.urls import reverse
import math

from ..models import Dispute, Task, Notification, RewardLedger, DisputeVote, DisputeAppeal


@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    primary_votes = dispute.votes.filter(tier='primary')
    poster_primary_votes = primary_votes.filter(vote='poster').count()
    taker_primary_votes = primary_votes.filter(vote='taker').count()
    total_primary_votes = primary_votes.count()

    senior_votes = dispute.votes.filter(tier='senior')
    poster_senior_votes = senior_votes.filter(vote='poster').count()
    taker_senior_votes = senior_votes.filter(vote='taker').count()
    total_senior_votes = senior_votes.count()

    user_primary_vote = primary_votes.filter(juror=request.user).first()
    user_senior_vote = senior_votes.filter(juror=request.user).first()

    is_litigant = (request.user == task.posted_by or request.user == task.taken_by)
    can_appeal = (
        dispute.status == 'primary_resolved' and
        dispute.is_in_appeal_window and
        is_litigant and
        not hasattr(dispute, 'appeal')
    )

    context = {
        'dispute': dispute,
        'task': task,
        'primary_votes': primary_votes,
        'poster_primary_votes': poster_primary_votes,
        'taker_primary_votes': taker_primary_votes,
        'total_primary_votes': total_primary_votes,
        'senior_votes': senior_votes,
        'poster_senior_votes': poster_senior_votes,
        'taker_senior_votes': taker_senior_votes,
        'total_senior_votes': total_senior_votes,
        'user_primary_vote': user_primary_vote,
        'user_senior_vote': user_senior_vote,
        'is_litigant': is_litigant,
        'can_appeal': can_appeal,
        'appeal': getattr(dispute, 'appeal', None),
        'appeal_bond_required': task.reward,
        'user_rewards': request.user.userprofile.rewards,
        'is_assigned_juror': request.user in dispute.assigned_jurors.all(),
        'is_juror_eligible': request.user.userprofile.is_juror_eligible,
    }
    return render(request, 'dispute_detail.html', context)


@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status in ['open', 'primary_resolved', 'appealed']:
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
                dispute.primary_outcome = None
                dispute.primary_resolved_at = None
                dispute.senior_outcome = None
                dispute.final_winner = None
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    deposit_amount=deposit_amount,
                    escrow_status='held'
                )

            # Assign up to 5 eligible neutral community jurors
            eligible_users = User.objects.exclude(id__in=[task.posted_by.id, request.user.id])
            now = timezone.now()
            eligible_jurors = [
                u for u in eligible_users
                if not (hasattr(u, 'userprofile') and u.userprofile.juror_ineligible_until and u.userprofile.juror_ineligible_until > now)
            ]
            assigned_jurors = eligible_jurors[:5]
            if assigned_jurors:
                dispute.assigned_jurors.set(assigned_jurors)

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
@require_POST
def vote_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    # Guardrail 1: Litigants cannot serve as jurors on their own disputed tasks
    if request.user == task.posted_by or request.user == task.taken_by:
        messages.error(request, "Litigants cannot serve as jurors on their own disputed tasks.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    # Check juror eligibility
    if not request.user.userprofile.is_juror_eligible:
        messages.error(request, "You are currently ineligible to serve as a juror.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    tier = request.POST.get('tier', 'primary')
    vote = request.POST.get('vote')
    reason = request.POST.get('reason', '')

    if vote not in ['poster', 'taker']:
        messages.error(request, "Invalid vote choice.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        if tier == 'primary':
            if dispute.status != 'open':
                messages.error(request, "Primary voting is no longer open for this dispute.")
                return redirect('dispute_detail', dispute_id=dispute.id)

            vote_obj, created = DisputeVote.objects.get_or_create(
                dispute=dispute,
                juror=request.user,
                tier='primary',
                defaults={'vote': vote, 'reason': reason}
            )
            if not created:
                messages.warning(request, "You have already voted in the primary phase.")
                return redirect('dispute_detail', dispute_id=dispute.id)

            messages.success(request, "Your vote has been recorded.")

            # Check primary consensus
            if dispute.check_primary_consensus():
                winner_label = "Task Poster" if dispute.primary_outcome == 'poster' else "Task Taker"
                messages.info(request, f"Primary voting quorum and 66% consensus supermajority reached! Decision in favor of {winner_label}. 48-hour appeal window is now active.")

                dispute_link = reverse('dispute_detail', args=[dispute.id])
                Notification.objects.create(
                    recipient=task.posted_by,
                    message=f"Primary jury consensus reached for task '{task.title}' ({winner_label} favored). You have 48 hours to appeal.",
                    link=dispute_link
                )
                if task.taken_by:
                    Notification.objects.create(
                        recipient=task.taken_by,
                        message=f"Primary jury consensus reached for task '{task.title}' ({winner_label} favored). You have 48 hours to appeal.",
                        link=dispute_link
                    )

        elif tier == 'senior':
            if dispute.status != 'appealed':
                messages.error(request, "Senior panel review is not active for this dispute.")
                return redirect('dispute_detail', dispute_id=dispute.id)

            vote_obj, created = DisputeVote.objects.get_or_create(
                dispute=dispute,
                juror=request.user,
                tier='senior',
                defaults={'vote': vote, 'reason': reason}
            )
            if not created:
                messages.warning(request, "You have already voted in the senior panel phase.")
                return redirect('dispute_detail', dispute_id=dispute.id)

            messages.success(request, "Your senior panel vote has been recorded.")

            senior_votes = dispute.votes.filter(tier='senior')
            total_senior = senior_votes.count()
            if total_senior >= 3:
                poster_s_votes = senior_votes.filter(vote='poster').count()
                taker_s_votes = senior_votes.filter(vote='taker').count()
                if poster_s_votes != taker_s_votes:
                    final_winner = 'poster' if poster_s_votes > taker_s_votes else 'taker'
                    dispute.senior_outcome = final_winner
                    dispute.save()
                    finalize_dispute_resolution(dispute, final_winner, is_appeal_outcome=True)
                    messages.info(request, f"Senior panel rendered binding final decision for {final_winner.title()}. Dispute settled.")

    return redirect('dispute_detail', dispute_id=dispute.id)


@login_required(login_url='/login/')
@require_POST
def appeal_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "Only task litigants can submit an appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if dispute.status != 'primary_resolved' or not dispute.is_in_appeal_window:
        messages.error(request, "The 48-hour appeal window for this dispute is closed or invalid.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if hasattr(dispute, 'appeal'):
        messages.error(request, "An appeal has already been filed for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    reason = request.POST.get('reason', '')
    if not reason:
        messages.error(request, "Please provide a reason for appealing.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    # Guardrail 2: Appeal bond amounts must equal 100% of the task reward amount
    bond_amount = task.reward
    user_profile = request.user.userprofile

    if user_profile.rewards < bond_amount:
        messages.error(
            request,
            f"Insufficient reward points. You need {bond_amount} points (100% of task reward) as an appeal bond, but you have {user_profile.rewards} points."
        )
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        user_profile.rewards -= bond_amount
        user_profile.save()

        DisputeAppeal.objects.create(
            dispute=dispute,
            appellant=request.user,
            bond_amount=bond_amount,
            reason=reason,
            status='pending'
        )

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-bond_amount,
            transaction_type='appeal_bond_held',
            description=f"Appeal bond escrow held for dispute on task: '{task.title}'"
        )

        dispute.status = 'appealed'
        dispute.save()

        counterparty = task.taken_by if request.user == task.posted_by else task.posted_by
        if counterparty:
            Notification.objects.create(
                recipient=counterparty,
                message=f"{request.user.username} has submitted a formal appeal for dispute on task '{task.title}'. Escalated to Senior Panel.",
                link=reverse('dispute_detail', args=[dispute.id])
            )

    messages.success(request, f"Appeal submitted successfully! {bond_amount} points held as appeal bond. Case escalated to Senior Panel.")
    return redirect('dispute_detail', dispute_id=dispute.id)


def finalize_dispute_resolution(dispute, final_winner, is_appeal_outcome=False):
    """
    Executes final settlement of task reward, deposit bond, appeal bond,
    and slashes bad-faith primary jurors if initial decision was overturned.
    """
    task = dispute.task
    with transaction.atomic():
        # 1. Settling Task Reward & Security Deposit Bond
        if final_winner == 'poster':
            task.status = 'cancelled'
            task.save()

            poster_profile = task.posted_by.userprofile
            poster_profile.rewards += task.reward
            poster_profile.save()

            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Refund for dispute resolution victory on task: '{task.title}'"
            )

            if dispute.escrow_status == 'held':
                if dispute.raised_by == task.posted_by:
                    dispute.refund_deposit(reason_description=f"Security deposit bond refunded for task '{task.title}' victory.")
                else:
                    dispute.forfeit_deposit(beneficiary=task.posted_by, reason_description=f"Security deposit bond forfeited by losing taker on task '{task.title}'.")

        elif final_winner == 'taker':
            task.status = 'completed'
            task.save()

            if task.taken_by:
                taker_profile = task.taken_by.userprofile
                taker_profile.rewards += task.reward
                taker_profile.save()

                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Awarded reward for dispute resolution victory on task: '{task.title}'"
                )

            if dispute.escrow_status == 'held':
                if dispute.raised_by == task.taken_by:
                    dispute.refund_deposit(reason_description=f"Security deposit bond refunded for task '{task.title}' victory.")
                else:
                    dispute.forfeit_deposit(beneficiary=task.taken_by, reason_description=f"Security deposit bond forfeited by losing poster on task '{task.title}'.")

        # 2. Settling Formal Appeal Bond & Slashing bad-faith voters if appealed
        if is_appeal_outcome and hasattr(dispute, 'appeal'):
            appeal = dispute.appeal
            winning_litigant = task.posted_by if final_winner == 'poster' else task.taken_by

            if appeal.appellant == winning_litigant:
                # Appeal Upheld / Successful Appeal
                appeal.status = 'upheld'
                appeal.save()

                # Refund 100% appeal bond to appellant
                appellant_profile = appeal.appellant.userprofile
                appellant_profile.rewards += appeal.bond_amount
                appellant_profile.save()

                RewardLedger.objects.create(
                    user=appeal.appellant,
                    task=task,
                    amount=appeal.bond_amount,
                    transaction_type='appeal_bond_refund',
                    description=f"Appeal bond refunded for successful appeal on task: '{task.title}'"
                )

                # Acceptance Criteria 5: Jurors who vote against final consensus on overturned decisions lose 100 reward points.
                bad_faith_primary_votes = dispute.votes.filter(tier='primary').exclude(vote=final_winner)
                for vote in bad_faith_primary_votes:
                    juror_profile = vote.juror.userprofile
                    juror_profile.rewards = max(0, juror_profile.rewards - 100)
                    juror_profile.save()

                    RewardLedger.objects.create(
                        user=vote.juror,
                        task=task,
                        amount=-100,
                        transaction_type='juror_slash',
                        description=f"Slashed 100 points for voting against final consensus on overturned dispute for task: '{task.title}'"
                    )

            else:
                # Appeal Rejected / Frivolous Appeal
                appeal.status = 'rejected'
                appeal.save()

                # Acceptance Criteria 4: Losing appellants forfeit 100% of their appeal bond to the winning counterparty and platform reserve.
                bond = appeal.bond_amount
                winner_share = math.floor(bond * 0.8)

                winning_profile = winning_litigant.userprofile
                winning_profile.rewards += winner_share
                winning_profile.save()

                RewardLedger.objects.create(
                    user=winning_litigant,
                    task=task,
                    amount=winner_share,
                    transaction_type='appeal_bond_forfeit',
                    description=f"Awarded share of forfeited appeal bond from defeated appellant for task: '{task.title}'"
                )

                RewardLedger.objects.create(
                    user=appeal.appellant,
                    task=task,
                    amount=0,
                    transaction_type='appeal_bond_forfeit',
                    description=f"Appeal bond forfeited for rejected appeal on task: '{task.title}'"
                )

        # 3. Reward Honest Jurors
        honest_votes = dispute.votes.filter(vote=final_winner)
        for vote in honest_votes:
            juror_profile = vote.juror.userprofile
            juror_profile.rewards += 20
            juror_profile.save()

            RewardLedger.objects.create(
                user=vote.juror,
                task=task,
                amount=20,
                transaction_type='juror_reward',
                description=f"Awarded 20 points for voting in consensus with final dispute outcome on task: '{task.title}'"
            )

        # Mark dispute resolved
        dispute.status = 'resolved'
        dispute.final_winner = final_winner
        dispute.save()

        # Send notifications
        dispute_link = reverse('dispute_detail', args=[dispute.id])
        winner_name = "Task Poster" if final_winner == 'poster' else "Task Taker"
        for recipient in [task.posted_by, task.taken_by]:
            if recipient:
                Notification.objects.create(
                    recipient=recipient,
                    message=f"Dispute for task '{task.title}' has been resolved in favor of {winner_name}.",
                    link=dispute_link
                )
