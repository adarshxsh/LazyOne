import json
import hashlib
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.views.decorators.http import require_POST
from django.urls import reverse
from django.http import JsonResponse
from django.utils import timezone
from ..models import Dispute, Task, Notification, RewardLedger, JurorVote

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    user_vote = JurorVote.objects.filter(dispute=dispute, juror=request.user).first()

    context = {
        'dispute': dispute,
        'task': task,
        'user_vote': user_vote,
        'is_voting_period': dispute.is_voting_period,
        'is_reveal_period': dispute.is_reveal_period,
        'is_reveal_completed': dispute.is_reveal_completed,
        'vote_tally': dispute.get_vote_tally() if dispute.is_reveal_completed else None
    }
    return render(request, 'dispute_detail.html', context)

def _get_request_data(request):
    if request.content_type == 'application/json':
        try:
            return json.loads(request.body)
        except Exception:
            return {}
    return request.POST

@login_required(login_url='/login/')
@require_POST
def submit_commitment(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    data = _get_request_data(request)
    commitment_hash = data.get('vote_commitment_hash') or data.get('commitment_hash') or data.get('hash')

    is_json = request.content_type == 'application/json' or request.headers.get('x-requested-with') == 'XMLHttpRequest'

    if not commitment_hash or len(str(commitment_hash).strip()) != 64:
        msg = "Invalid or missing SHA-256 vote commitment hash."
        if is_json:
            return JsonResponse({'error': msg}, status=400)
        messages.error(request, msg)
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not dispute.is_voting_period:
        msg = "Dispute is not currently in the voting period."
        if is_json:
            return JsonResponse({'error': msg}, status=400)
        messages.error(request, msg)
        return redirect('dispute_detail', dispute_id=dispute.id)

    juror_vote, created = JurorVote.objects.get_or_create(
        dispute=dispute,
        juror=request.user,
        defaults={
            'vote_commitment_hash': str(commitment_hash).strip(),
            'is_revealed': False,
            'revealed_vote': None
        }
    )
    if not created:
        if juror_vote.is_revealed:
            msg = "Vote has already been revealed and cannot be changed."
            if is_json:
                return JsonResponse({'error': msg}, status=400)
            messages.error(request, msg)
            return redirect('dispute_detail', dispute_id=dispute.id)
        juror_vote.vote_commitment_hash = str(commitment_hash).strip()
        juror_vote.save()

    msg = "Vote commitment successfully stored."
    if is_json:
        return JsonResponse({'message': msg, 'status': 'success', 'vote_commitment_hash': juror_vote.vote_commitment_hash})
    messages.success(request, msg)
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def reveal_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    data = _get_request_data(request)

    vote_choice = data.get('vote') or data.get('vote_choice') or data.get('choice')
    salt = data.get('salt')

    is_json = request.content_type == 'application/json' or request.headers.get('x-requested-with') == 'XMLHttpRequest'

    if not salt or len(str(salt)) < 8:
        msg = "Salt string must be at least 8 characters long."
        if is_json:
            return JsonResponse({'error': msg}, status=400)
        messages.error(request, msg)
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not vote_choice:
        msg = "Vote choice is required."
        if is_json:
            return JsonResponse({'error': msg}, status=400)
        messages.error(request, msg)
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not dispute.is_reveal_period:
        msg = "Dispute is not currently in the reveal period."
        if is_json:
            return JsonResponse({'error': msg}, status=400)
        messages.error(request, msg)
        return redirect('dispute_detail', dispute_id=dispute.id)

    juror_vote = JurorVote.objects.filter(dispute=dispute, juror=request.user).first()
    if not juror_vote or not juror_vote.vote_commitment_hash:
        msg = "No vote commitment found for this dispute."
        if is_json:
            return JsonResponse({'error': msg}, status=400)
        messages.error(request, msg)
        return redirect('dispute_detail', dispute_id=dispute.id)

    # Verify SHA256(choice + salt) == vote_commitment_hash
    combined = f"{vote_choice}{salt}"
    calculated_hash = hashlib.sha256(combined.encode('utf-8')).hexdigest()

    if calculated_hash.lower() != juror_vote.vote_commitment_hash.strip().lower():
        msg = "SHA-256 hash mismatch: vote choice and salt do not match commitment."
        if is_json:
            return JsonResponse({'error': msg}, status=400)
        messages.error(request, msg)
        return redirect('dispute_detail', dispute_id=dispute.id)

    juror_vote.revealed_vote = str(vote_choice)
    juror_vote.is_revealed = True
    juror_vote.revealed_at = timezone.now()
    juror_vote.save()

    msg = "Vote successfully revealed and verified."
    if is_json:
        return JsonResponse({'message': msg, 'status': 'success', 'revealed_vote': juror_vote.revealed_vote})
    messages.success(request, msg)
    return redirect('dispute_detail', dispute_id=dispute.id)

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
