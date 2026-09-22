import math
from datetime import timedelta
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.urls import reverse
from ..models import Dispute, Task, Notification, RewardLedger, UserProfile, JuryPanel, JurorVote, DisputeAppeal

def execute_final_dispute_settlement(dispute, winning_party):
    task = dispute.task
    slashed_occurred = False

    with transaction.atomic():
        # 1. Task settlement based on winning party
        if winning_party == task.taken_by:
            if task.taken_by:
                taker_profile, _ = UserProfile.objects.get_or_create(user=task.taken_by)
                taker_profile.rewards += task.reward
                taker_profile.save()
                RewardLedger.objects.create(
                    user=task.taken_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_completion',
                    description=f"Awarded reward for dispute resolution on task: '{task.title}'"
                )
            task.status = 'completed'
            task.save()
        else:
            poster_profile, _ = UserProfile.objects.get_or_create(user=task.posted_by)
            poster_profile.rewards += task.reward
            poster_profile.save()
            RewardLedger.objects.create(
                user=task.posted_by,
                task=task,
                amount=task.reward,
                transaction_type='task_cancellation',
                description=f"Refund for dispute resolution on task: '{task.title}'"
            )
            task.status = 'cancelled'
            task.save()

        # 2. Litigant Escrow Bond Settlement
        if dispute.escrow_status == 'held':
            if dispute.raised_by == winning_party:
                dispute.refund_deposit(reason_description=f"Security deposit bond refunded upon favorable dispute ruling for task '{task.title}'")
            else:
                slashed_occurred = True
                dispute.escrow_status = 'forfeited'
                dispute.save()
                RewardLedger.objects.create(
                    user=dispute.raised_by,
                    task=task,
                    amount=-dispute.deposit_amount,
                    transaction_type='litigant_slashing',
                    description=f"Dishonest litigant deposit bond slashed for task: '{task.title}'"
                )

        # 3. Appeal Bond Settlement (if any appeal exists)
        for appeal in dispute.appeals.all():
            if appeal.appellant == winning_party:
                appellant_profile, _ = UserProfile.objects.get_or_create(user=appeal.appellant)
                appellant_profile.rewards += appeal.appeal_bond_amount
                appellant_profile.save()
                RewardLedger.objects.create(
                    user=appeal.appellant,
                    task=task,
                    amount=appeal.appeal_bond_amount,
                    transaction_type='appeal_refund',
                    description=f"Appeal deposit bond refunded for successful appeal on task '{task.title}'"
                )
                appeal.status = 'overturned'
                appeal.save()
            else:
                slashed_occurred = True
                RewardLedger.objects.create(
                    user=appeal.appellant,
                    task=task,
                    amount=-appeal.appeal_bond_amount,
                    transaction_type='litigant_slashing',
                    description=f"Dishonest appellant appeal deposit bond slashed for task '{task.title}'"
                )
                appeal.status = 'upheld'
                appeal.save()

        # 4. Check if any juror slashing occurred across jury panels
        for panel in dispute.jury_panels.all():
            total_votes = panel.votes.count()
            winning_votes = panel.votes.filter(voted_for=winning_party).count()
            if total_votes > 0 and (winning_votes / total_votes) > 0.80 and panel.votes.exclude(voted_for=winning_party).exists():
                slashed_occurred = True

        if slashed_occurred:
            dispute.status = 'slashed'
        else:
            dispute.status = 'resolved'
        dispute.save()

        dispute_link = reverse('dispute_detail', args=[dispute.id])
        for participant in [task.posted_by, task.taken_by]:
            if participant:
                Notification.objects.create(
                    recipient=participant,
                    message=f"Final dispute ruling issued for task '{task.title}'. Winner: {winning_party.username}.",
                    link=dispute_link
                )

def process_panel_settlement(panel, winning_party):
    dispute = panel.dispute
    task = dispute.task
    now = timezone.now()

    with transaction.atomic():
        panel.status = 'resolved'
        panel.resolved_at = now
        panel.save()

        total_votes = panel.votes.count()
        winning_votes = panel.votes.filter(voted_for=winning_party).count()
        consensus_ratio = (winning_votes / total_votes) if total_votes > 0 else 0

        # Requirement 3: Jurors voting against overwhelming consensus (> 80%) lose their locked 10 point stake via dispute_slash transaction.
        # Slashing only applies when consensus threshold exceeds 80% majority.
        overwhelming_consensus = consensus_ratio >= 0.80

        for vote in panel.votes.all():
            juror_profile, _ = UserProfile.objects.get_or_create(user=vote.juror)
            if vote.voted_for == winning_party:
                reward_amt = 20
                juror_profile.rewards += reward_amt
                juror_profile.save()
                RewardLedger.objects.create(
                    user=vote.juror,
                    task=task,
                    amount=reward_amt,
                    transaction_type='juror_reward',
                    description=f"Juror reward for consensus vote on task '{task.title}'"
                )
            else:
                # Dissenting vote
                if overwhelming_consensus:
                    penalty_amt = 10
                    juror_profile.rewards = max(0, juror_profile.rewards - penalty_amt)
                    juror_profile.save()
                    RewardLedger.objects.create(
                        user=vote.juror,
                        task=task,
                        amount=-penalty_amt,
                        transaction_type='dispute_slash',
                        description=f"Juror stake slashed for voting against overwhelming consensus on task '{task.title}'"
                    )
                    # Transfer slashed points to winning party
                    winning_profile, _ = UserProfile.objects.get_or_create(user=winning_party)
                    winning_profile.rewards += penalty_amt
                    winning_profile.save()

        if panel.tier == 1:
            dispute.status = 'appeal_period'
            dispute.save()

            dispute_link = reverse('dispute_detail', args=[dispute.id])
            for participant in [task.posted_by, task.taken_by]:
                if participant:
                    Notification.objects.create(
                        recipient=participant,
                        message=f"Tier-1 Jury Panel has ruled in favor of {winning_party.username} for task '{task.title}'. A 48-hour appeal window is now open.",
                        link=dispute_link
                    )
        elif panel.tier == 2:
            execute_final_dispute_settlement(dispute, winning_party)

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff and not request.user.jury_panels.filter(dispute=dispute).exists():
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    active_panel = dispute.jury_panels.filter(status='active').order_by('-tier', '-created_at').first()
    user_is_active_juror = False
    user_has_voted = False
    if active_panel:
        user_is_active_juror = request.user in active_panel.jurors.all()
        user_has_voted = JurorVote.objects.filter(panel=active_panel, juror=request.user).exists()

    context = {
        'dispute': dispute,
        'task': task,
        'active_panel': active_panel,
        'user_is_active_juror': user_is_active_juror,
        'user_has_voted': user_has_voted,
        'can_be_appealed': dispute.can_be_appealed,
        'appeal_bond_amount': task.deposit_bond_amount * 2,
        'all_panels': dispute.jury_panels.all().order_by('tier'),
        'appeals': dispute.appeals.all(),
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
def raise_dispute(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    if hasattr(task, 'dispute') and task.dispute.status in ['open', 'peer_review', 'appeal_period']:
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
                dispute.status = 'peer_review'
                dispute.deposit_amount = deposit_amount
                dispute.escrow_status = 'held'
                dispute.save()
            else:
                dispute = Dispute.objects.create(
                    task=task,
                    raised_by=request.user,
                    reason=reason,
                    status='peer_review',
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

            # Assign Tier-1 Jury Panel
            panel = JuryPanel.objects.create(
                dispute=dispute,
                tier=1,
                quorum_size=3,
                status='active'
            )
            panel.assign_eligible_jurors()

            Notification.objects.create(
                recipient=task.posted_by,
                message=f"{request.user.username} has raised a dispute for your task: '{task.title}'.",
                link=reverse('dispute_detail', args=[dispute.id])
            )
        messages.success(request, f"Dispute raised successfully. {deposit_amount} points held as deposit bond. Tier-1 Peer Jury assigned.")
        return redirect('dispute_detail', dispute_id=dispute.id)
    return redirect('my_tasks')

@login_required(login_url='/login/')
@require_POST
def cast_juror_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    panel = dispute.jury_panels.filter(status='active').order_by('-tier', '-created_at').first()
    if not panel:
        messages.error(request, "There is no active jury panel for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.user not in panel.jurors.all():
        messages.error(request, "You are not an assigned juror for this panel.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if JurorVote.objects.filter(panel=panel, juror=request.user).exists():
        messages.error(request, "You have already cast your vote for this panel.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    voted_for_id = request.POST.get('voted_for')
    justification = request.POST.get('justification', '')

    task = dispute.task
    if str(voted_for_id) == str(task.posted_by.id):
        voted_for_user = task.posted_by
    elif task.taken_by and str(voted_for_id) == str(task.taken_by.id):
        voted_for_user = task.taken_by
    else:
        messages.error(request, "Invalid vote selection.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        JurorVote.objects.create(
            panel=panel,
            juror=request.user,
            voted_for=voted_for_user,
            justification=justification
        )

        winning_party = panel.evaluate_consensus()
        if winning_party:
            process_panel_settlement(panel, winning_party)

    messages.success(request, "Your juror vote has been recorded successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def file_dispute_appeal(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    if request.user != task.posted_by and request.user != task.taken_by:
        messages.error(request, "Only parties involved in the dispute can file an appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    # Constraint: Maximum 1 escalation tier
    if dispute.appeals.exists():
        messages.error(request, "This dispute has already been escalated. Maximum 1 appeal escalation tier allowed.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not dispute.can_be_appealed:
        messages.error(request, "This dispute is not eligible for appeal or the appeal window has expired.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    justification = request.POST.get('justification', '')
    if not justification:
        messages.error(request, "Justification is required to file an appeal.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    # Requirement 2: 2x deposit bond
    appeal_bond_amount = task.deposit_bond_amount * 2
    user_profile, _ = UserProfile.objects.get_or_create(user=request.user)

    if user_profile.rewards < appeal_bond_amount:
        messages.error(
            request,
            f"Insufficient reward points balance. You need at least {appeal_bond_amount} points as a 2x deposit bond to file an appeal."
        )
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        user_profile.rewards -= appeal_bond_amount
        user_profile.save()

        RewardLedger.objects.create(
            user=request.user,
            task=task,
            amount=-appeal_bond_amount,
            transaction_type='appeal_deposit',
            description=f"Appeal deposit bond held for Tier-2 appeal on task: '{task.title}'"
        )

        DisputeAppeal.objects.create(
            dispute=dispute,
            appellant=request.user,
            appeal_bond_amount=appeal_bond_amount,
            justification=justification,
            status='pending'
        )

        # Transition dispute to appeal_period
        dispute.status = 'appeal_period'
        dispute.save()

        # Requirement 2 / User Scenario: Expanded 5-juror senior panel
        panel = JuryPanel.objects.create(
            dispute=dispute,
            tier=2,
            quorum_size=5,
            status='active'
        )
        panel.assign_eligible_jurors()

        tier1_panel = dispute.jury_panels.filter(tier=1).first()
        if tier1_panel:
            tier1_panel.status = 'escalated'
            tier1_panel.save()

        dispute_link = reverse('dispute_detail', args=[dispute.id])
        other_party = task.posted_by if request.user == task.taken_by else task.taken_by
        if other_party:
            Notification.objects.create(
                recipient=other_party,
                message=f"{request.user.username} has escalated the dispute for task '{task.title}' to a Tier-2 Senior Jury Panel.",
                link=dispute_link
            )

    messages.success(request, f"Appeal filed successfully. {appeal_bond_amount} points held as 2x appeal bond. Tier-2 Senior Jury Panel assigned.")
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
