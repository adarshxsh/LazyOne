import json
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse, HttpResponseBadRequest
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger
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
def partial_settle_dispute(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    is_json = (
        request.headers.get('x-requested-with') == 'XMLHttpRequest' or
        request.content_type == 'application/json' or
        request.POST.get('format') == 'json'
    )

    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff and not request.user.is_superuser:
        if is_json:
            return JsonResponse({'error': 'You are not authorized to settle this dispute.'}, status=403)
        messages.error(request, "You are not authorized to settle this dispute.")
        return redirect('home')

    if dispute.status != 'open' or task.status != 'disputed':
        if is_json:
            return JsonResponse({'error': 'Dispute is not open or task is not in disputed status.'}, status=400)
        messages.error(request, "Dispute is not open or task is not in disputed status.")
        return redirect('dispute_detail', dispute_id=dispute.id)

    # Parse POST parameters or JSON body
    data = {}
    if request.content_type == 'application/json':
        try:
            data = json.loads(request.body.decode('utf-8'))
        except Exception:
            data = {}
    else:
        data = request.POST

    payout_amount = None
    refund_amount = None

    # Check for percentage-based split input
    pct_val = None
    for key in ['percentage', 'payout_percentage', 'taker_percentage', 'split_percentage']:
        if key in data and data[key] is not None and str(data[key]).strip() != '':
            pct_val = data[key]
            break

    if pct_val is not None:
        try:
            pct_str = str(pct_val).rstrip('%').strip()
            pct = float(pct_str)
            if pct < 0 or pct > 100:
                err_msg = "Percentage must be between 0 and 100."
                if is_json:
                    return JsonResponse({'error': err_msg}, status=400)
                messages.error(request, err_msg)
                return redirect('dispute_detail', dispute_id=dispute.id)
            payout_amount = int(round(task.reward * (pct / 100.0)))
            refund_amount = task.reward - payout_amount
        except (ValueError, TypeError):
            err_msg = "Invalid percentage value."
            if is_json:
                return JsonResponse({'error': err_msg}, status=400)
            messages.error(request, err_msg)
            return redirect('dispute_detail', dispute_id=dispute.id)
    else:
        # Check for explicit point split inputs
        payout_key_val = None
        for key in ['payout_amount', 'payout', 'taker_points', 'payout_points']:
            if key in data and data[key] is not None and str(data[key]).strip() != '':
                payout_key_val = data[key]
                break

        refund_key_val = None
        for key in ['refund_amount', 'refund', 'poster_points', 'refund_points']:
            if key in data and data[key] is not None and str(data[key]).strip() != '':
                refund_key_val = data[key]
                break

        try:
            if payout_key_val is not None and refund_key_val is not None:
                payout_amount = int(payout_key_val)
                refund_amount = int(refund_key_val)
                if payout_amount < 0 or refund_amount < 0:
                    raise ValueError("Split values cannot be negative.")
                if payout_amount + refund_amount != task.reward:
                    err_msg = f"Sum of payout ({payout_amount}) and refund ({refund_amount}) must equal total task reward ({task.reward})."
                    if is_json:
                        return JsonResponse({'error': err_msg}, status=400)
                    messages.error(request, err_msg)
                    return redirect('dispute_detail', dispute_id=dispute.id)
            elif payout_key_val is not None:
                payout_amount = int(payout_key_val)
                if payout_amount < 0 or payout_amount > task.reward:
                    raise ValueError("Payout amount out of bounds.")
                refund_amount = task.reward - payout_amount
            elif refund_key_val is not None:
                refund_amount = int(refund_key_val)
                if refund_amount < 0 or refund_amount > task.reward:
                    raise ValueError("Refund amount out of bounds.")
                payout_amount = task.reward - refund_amount
            else:
                err_msg = "Please specify percentage or point split values."
                if is_json:
                    return JsonResponse({'error': err_msg}, status=400)
                messages.error(request, err_msg)
                return redirect('dispute_detail', dispute_id=dispute.id)
        except (ValueError, TypeError) as e:
            err_msg = str(e) if str(e) else "Invalid point split values."
            if is_json:
                return JsonResponse({'error': err_msg}, status=400)
            messages.error(request, err_msg)
            return redirect('dispute_detail', dispute_id=dispute.id)

    # Double check bounds
    if payout_amount < 0 or refund_amount < 0 or payout_amount > task.reward or refund_amount > task.reward:
        err_msg = "Split values must be non-negative and cannot exceed total reward."
        if is_json:
            return JsonResponse({'error': err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect('dispute_detail', dispute_id=dispute.id)

    with transaction.atomic():
        dispute.refund_deposit(
            reason_description=f"Security deposit bond refunded upon partial dispute settlement for task: '{task.title}'"
        )
        if task.taken_by:
            taker_profile = task.taken_by.userprofile
            taker_profile.rewards += payout_amount
            taker_profile.save()
            RewardLedger.objects.create(
                user=task.taken_by,
                task=task,
                amount=payout_amount,
                transaction_type='partial_dispute_payout',
                description=f"Partial dispute payout for task: '{task.title}'"
            )

        poster_profile = task.posted_by.userprofile
        poster_profile.rewards += refund_amount
        poster_profile.save()
        RewardLedger.objects.create(
            user=task.posted_by,
            task=task,
            amount=refund_amount,
            transaction_type='partial_dispute_refund',
            description=f"Partial dispute refund for task: '{task.title}'"
        )

        dispute.status = 'resolved'
        dispute.save()
        task.status = 'completed'
        task.save()

        if task.taken_by:
            Notification.objects.create(
                recipient=task.taken_by,
                message=f"Dispute for task '{task.title}' settled: {payout_amount} points paid out to you, {refund_amount} points refunded to poster.",
                link=reverse('my_tasks')
            )
        Notification.objects.create(
            recipient=task.posted_by,
            message=f"Dispute for task '{task.title}' settled: {refund_amount} points refunded to you, {payout_amount} points paid out to taker.",
            link=reverse('my_tasks')
        )

    if is_json:
        return JsonResponse({
            'status': 'success',
            'message': f"Dispute settled: {payout_amount} points paid out to taker, {refund_amount} points refunded to poster.",
            'payout_amount': payout_amount,
            'refund_amount': refund_amount,
            'task_id': task.id,
            'dispute_id': dispute.id
        })

    messages.success(request, f"Dispute settled successfully! {payout_amount} points paid out to taker, {refund_amount} points refunded to poster.")
    return redirect('dispute_detail', dispute_id=dispute.id)

