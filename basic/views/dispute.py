from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.http import JsonResponse, HttpResponseForbidden
from django.contrib.auth.models import User
from ..models import Dispute, Task, Notification, RewardLedger, DisputeVote
from django.views.decorators.http import require_POST
from django.urls import reverse

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')
    context = {
        'dispute': dispute,
        'task': task
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

    is_json = request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept', '')

    # Check 1: Dispute must be open
    if dispute.status != 'open':
        if is_json:
            return JsonResponse({'status': 'error', 'message': 'Jury voting is only allowed for open disputes.'}, status=400)
        messages.error(request, "Jury voting is only allowed for open disputes.")
        if hasattr(task, 'conversation') and task.conversation:
            return redirect('chat_view', conversation_id=task.conversation.id)
        return redirect('home')

    # Check 2: Voter neutrality (task poster and taker are barred from voting)
    if request.user == task.posted_by or request.user == task.taken_by:
        if is_json:
            return JsonResponse({'status': 'error', 'message': 'Task poster and taker cannot vote on their own dispute.'}, status=403)
        messages.error(request, "Task poster and taker cannot vote on their own dispute.")
        if hasattr(task, 'conversation') and task.conversation:
            return redirect('chat_view', conversation_id=task.conversation.id)
        return redirect('home')

    # Check 3: Candidate selection
    voted_for_id = request.POST.get('voted_for_id') or request.POST.get('voted_for') or request.POST.get('candidate_id')
    if not voted_for_id:
        if is_json:
            return JsonResponse({'status': 'error', 'message': 'A valid candidate selection is required.'}, status=400)
        messages.error(request, "A valid candidate selection is required.")
        if hasattr(task, 'conversation') and task.conversation:
            return redirect('chat_view', conversation_id=task.conversation.id)
        return redirect('home')

    try:
        voted_for = User.objects.get(id=voted_for_id)
    except User.DoesNotExist:
        if is_json:
            return JsonResponse({'status': 'error', 'message': 'Selected candidate user does not exist.'}, status=400)
        messages.error(request, "Selected candidate user does not exist.")
        if hasattr(task, 'conversation') and task.conversation:
            return redirect('chat_view', conversation_id=task.conversation.id)
        return redirect('home')

    if voted_for != task.posted_by and voted_for != task.taken_by:
        if is_json:
            return JsonResponse({'status': 'error', 'message': 'You can only vote for either the task poster or the task taker.'}, status=400)
        messages.error(request, "You can only vote for either the task poster or the task taker.")
        if hasattr(task, 'conversation') and task.conversation:
            return redirect('chat_view', conversation_id=task.conversation.id)
        return redirect('home')

    # Check 4: Duplicate voting prevention
    if DisputeVote.objects.filter(dispute=dispute, voter=request.user).exists():
        if is_json:
            return JsonResponse({'status': 'error', 'message': 'You have already voted on this dispute.'}, status=400)
        messages.error(request, "You have already voted on this dispute.")
        if hasattr(task, 'conversation') and task.conversation:
            return redirect('chat_view', conversation_id=task.conversation.id)
        return redirect('home')

    # Record vote and evaluate consensus inside atomic transaction
    with transaction.atomic():
        try:
            vote = DisputeVote.objects.create(
                dispute=dispute,
                voter=request.user,
                voted_for=voted_for
            )
        except Exception:
            if is_json:
                return JsonResponse({'status': 'error', 'message': 'You have already voted on this dispute.'}, status=400)
            messages.error(request, "You have already voted on this dispute.")
            if hasattr(task, 'conversation') and task.conversation:
                return redirect('chat_view', conversation_id=task.conversation.id)
            return redirect('home')

        # Re-tally votes
        votes = dispute.votes.select_related('voted_for').all()
        poster_votes = sum(1 for v in votes if v.voted_for == task.posted_by)
        taker_votes = sum(1 for v in votes if v.voted_for == task.taken_by)

        JURY_VOTE_THRESHOLD = 3
        resolved_now = False

        if poster_votes >= JURY_VOTE_THRESHOLD or taker_votes >= JURY_VOTE_THRESHOLD:
            dispute.status = 'resolved'
            dispute.save()
            resolved_now = True

            if poster_votes >= JURY_VOTE_THRESHOLD:
                # Poster wins: Refund task reward to poster, set task cancelled, and handle deposit bond
                poster_profile = task.posted_by.userprofile
                poster_profile.rewards += task.reward
                poster_profile.save()

                task.status = 'cancelled'
                task.save()

                RewardLedger.objects.create(
                    user=task.posted_by,
                    task=task,
                    amount=task.reward,
                    transaction_type='task_cancellation',
                    description=f"Refund awarded for dispute jury consensus reached for task: '{task.title}'"
                )

                if dispute.escrow_status == 'held':
                    if dispute.raised_by == task.posted_by:
                        dispute.refund_deposit(reason_description=f"Deposit bond refunded upon jury consensus for task: '{task.title}'")
                    else:
                        dispute.forfeit_deposit(beneficiary=task.posted_by, reason_description=f"Deposit bond forfeited to poster upon jury consensus for task: '{task.title}'")

            elif taker_votes >= JURY_VOTE_THRESHOLD:
                # Taker wins: Award task reward to taker, set task completed, and handle deposit bond
                if task.taken_by:
                    taker_profile = task.taken_by.userprofile
                    taker_profile.rewards += task.reward
                    taker_profile.save()

                    RewardLedger.objects.create(
                        user=task.taken_by,
                        task=task,
                        amount=task.reward,
                        transaction_type='task_completion',
                        description=f"Task reward awarded for dispute jury consensus reached for task: '{task.title}'"
                    )

                task.status = 'completed'
                task.save()

                if dispute.escrow_status == 'held':
                    if dispute.raised_by == task.taken_by:
                        dispute.refund_deposit(reason_description=f"Deposit bond refunded upon jury consensus for task: '{task.title}'")
                    else:
                        dispute.forfeit_deposit(beneficiary=task.taken_by, reason_description=f"Deposit bond forfeited to taker upon jury consensus for task: '{task.title}'")

            # Notify participants
            dispute_link = reverse('chat_view', args=[task.conversation.id]) if hasattr(task, 'conversation') and task.conversation else reverse('dispute_detail', args=[dispute.id])
            for participant in [task.posted_by, task.taken_by]:
                if participant:
                    Notification.objects.create(
                        recipient=participant,
                        message=f"Dispute for task '{task.title}' has been resolved by peer jury vote.",
                        link=dispute_link
                    )

    if is_json:
        return JsonResponse({
            'status': 'success',
            'message': 'Vote recorded successfully.',
            'poster_votes': poster_votes,
            'taker_votes': taker_votes,
            'dispute_status': dispute.status,
            'resolved': resolved_now
        })

    if resolved_now:
        messages.success(request, f"Your vote has been cast. Dispute has reached jury consensus and was resolved!")
    else:
        messages.success(request, "Your vote has been recorded successfully.")

    if hasattr(task, 'conversation') and task.conversation:
        return redirect('chat_view', conversation_id=task.conversation.id)
    return redirect('dispute_detail', dispute_id=dispute.id)
