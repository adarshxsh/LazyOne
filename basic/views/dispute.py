from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from ..models import Dispute, Task, Notification, RewardLedger, DisputeCommitment
from django.views.decorators.http import require_POST
from django.urls import reverse
import json

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task
    can_vote = dispute.can_user_vote(request.user)
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff and not can_vote:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    current_phase = dispute.get_current_phase()
    user_commitment = DisputeCommitment.objects.filter(dispute=dispute, juror=request.user).first() if request.user.is_authenticated else None
    has_committed = bool(user_commitment)
    has_revealed = user_commitment.revealed if user_commitment else False

    # Requirement 3: Withhold all vote counts and choices until the Reveal Phase finishes
    if current_phase == 'finished':
        tallies = dispute.tally_votes()
        revealed_votes = dispute.commitments.filter(revealed=True)
    else:
        tallies = None
        revealed_votes = None

    if request.headers.get('Accept') == 'application/json' or request.GET.get('format') == 'json':
        response_data = {
            'dispute_id': dispute.id,
            'status': dispute.status,
            'phase': current_phase,
            'user_status': {
                'can_vote': can_vote,
                'has_committed': has_committed,
                'has_revealed': has_revealed,
            },
            'tallies': tallies,
            'revealed_votes': list(revealed_votes.values('juror__username', 'vote_choice')) if revealed_votes else None,
        }
        return JsonResponse(response_data)

    context = {
        'dispute': dispute,
        'task': task,
        'current_phase': current_phase,
        'can_vote': can_vote,
        'user_commitment': user_commitment,
        'has_committed': has_committed,
        'has_revealed': has_revealed,
        'tallies': tallies,
        'revealed_votes': revealed_votes,
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
@require_POST
def commit_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    if not dispute.can_user_vote(request.user):
        if request.headers.get('Accept') == 'application/json' or request.content_type == 'application/json':
            return JsonResponse({'status': 'error', 'message': 'You are not authorized to vote on this dispute.'}, status=403)
        messages.error(request, "You are not authorized to vote on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    phase = dispute.get_current_phase()
    if phase != 'commit':
        if request.headers.get('Accept') == 'application/json' or request.content_type == 'application/json':
            return JsonResponse({'status': 'error', 'message': f'Commitments are not accepted during the {phase} phase.'}, status=400)
        messages.error(request, f"Commitments are not accepted during the {phase} phase.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.content_type == 'application/json':
        try:
            data = json.loads(request.body)
            commitment_hash = data.get('commitment_hash') or data.get('commitment')
        except Exception:
            commitment_hash = None
    else:
        commitment_hash = request.POST.get('commitment_hash') or request.POST.get('commitment')

    if not commitment_hash or not isinstance(commitment_hash, str) or len(commitment_hash.strip()) < 32:
        if request.headers.get('Accept') == 'application/json' or request.content_type == 'application/json':
            return JsonResponse({'status': 'error', 'message': 'A valid SHA-256 commitment hash is required.'}, status=400)
        messages.error(request, "A valid SHA-256 commitment hash is required.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    commitment_hash = commitment_hash.strip().lower()

    commitment, created = DisputeCommitment.objects.get_or_create(
        dispute=dispute,
        juror=request.user,
        defaults={'commitment_hash': commitment_hash}
    )
    if not created:
        commitment.commitment_hash = commitment_hash
        commitment.save()

    if request.headers.get('Accept') == 'application/json' or request.content_type == 'application/json':
        return JsonResponse({
            'status': 'success',
            'message': 'Vote commitment submitted successfully.',
            'commitment_hash': commitment_hash
        })

    messages.success(request, "Vote commitment submitted successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
@require_POST
def reveal_vote(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    if not dispute.can_user_vote(request.user):
        if request.headers.get('Accept') == 'application/json' or request.content_type == 'application/json':
            return JsonResponse({'status': 'error', 'message': 'You are not authorized to vote on this dispute.'}, status=403)
        messages.error(request, "You are not authorized to vote on this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    phase = dispute.get_current_phase()
    if phase != 'reveal':
        if request.headers.get('Accept') == 'application/json' or request.content_type == 'application/json':
            return JsonResponse({'status': 'error', 'message': f'Vote reveals are not accepted during the {phase} phase.'}, status=400)
        messages.error(request, f"Vote reveals are not accepted during the {phase} phase.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if request.content_type == 'application/json':
        try:
            data = json.loads(request.body)
            vote_choice = data.get('vote_choice') or data.get('choice')
            salt = data.get('salt')
        except Exception:
            vote_choice, salt = None, None
    else:
        vote_choice = request.POST.get('vote_choice') or request.POST.get('choice')
        salt = request.POST.get('salt')

    if not vote_choice or not salt:
        if request.headers.get('Accept') == 'application/json' or request.content_type == 'application/json':
            return JsonResponse({'status': 'error', 'message': 'Both vote choice and secret salt are required.'}, status=400)
        messages.error(request, "Both vote choice and secret salt are required.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    commitment = DisputeCommitment.objects.filter(dispute=dispute, juror=request.user).first()
    if not commitment:
        if request.headers.get('Accept') == 'application/json' or request.content_type == 'application/json':
            return JsonResponse({'status': 'error', 'message': 'No vote commitment was found for this dispute.'}, status=400)
        messages.error(request, "No vote commitment was found for this dispute.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    if not commitment.verify_reveal(vote_choice, salt):
        if request.headers.get('Accept') == 'application/json' or request.content_type == 'application/json':
            return JsonResponse({'status': 'error', 'message': 'Verification failed: vote choice and salt do not match stored commitment hash.'}, status=400)
        messages.error(request, "Verification failed: vote choice and salt do not match stored commitment hash.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    commitment.vote_choice = vote_choice
    commitment.salt = salt
    commitment.revealed = True
    commitment.revealed_at = timezone.now()
    commitment.save()

    if request.headers.get('Accept') == 'application/json' or request.content_type == 'application/json':
        return JsonResponse({
            'status': 'success',
            'message': 'Vote revealed and verified successfully.',
            'revealed': True
        })

    messages.success(request, "Vote revealed and verified successfully.")
    return redirect('dispute_detail', dispute_id=dispute.id)

@login_required(login_url='/login/')
def dispute_status_api(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    current_phase = dispute.get_current_phase()

    commitment = DisputeCommitment.objects.filter(dispute=dispute, juror=request.user).first() if request.user.is_authenticated else None
    user_status = {
        'can_vote': dispute.can_user_vote(request.user),
        'has_committed': bool(commitment),
        'has_revealed': commitment.revealed if commitment else False
    }

    response_data = {
        'dispute_id': dispute.id,
        'status': dispute.status,
        'phase': current_phase,
        'user_status': user_status,
        'total_commitments': dispute.commitments.count(),
        'total_revealed': dispute.commitments.filter(revealed=True).count(),
    }

    # Requirement 3: Withhold all vote counts and choices until the Reveal Phase finishes
    if current_phase == 'finished':
        response_data['tallies'] = dispute.tally_votes()
        response_data['revealed_votes'] = list(
            dispute.commitments.filter(revealed=True).values('juror__username', 'vote_choice')
        )
    else:
        response_data['tallies'] = None
        response_data['revealed_votes'] = None

    return JsonResponse(response_data)

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
