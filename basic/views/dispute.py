import logging
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from ..models import Dispute, Task, Notification, RewardLedger
from django.views.decorators.http import require_POST
from django.urls import reverse
import firebase_admin
from firebase_admin import firestore
from ..firebase_init import initialize_firebase

logger = logging.getLogger(__name__)

def sync_dispute_to_firestore(dispute, status=None, delete=False):
    """
    Safely pushes dispute status changes or document deletion to Firestore.
    Fails gracefully if Firebase Admin SDK is not initialized or network error occurs.
    """
    try:
        initialize_firebase()
        if not firebase_admin._apps:
            logger.warning("Firebase Admin SDK is not initialized. Skipping Firestore dispute sync.")
            return

        db = firestore.client()
        doc_ref = db.collection('disputes').document(str(dispute.id))

        if delete:
            # Update status to 'withdrawn' first so client listeners get event, then delete doc
            doc_ref.set({
                'id': dispute.id,
                'task_id': dispute.task.id,
                'status': 'withdrawn',
                'updated_at': firestore.SERVER_TIMESTAMP
            }, merge=True)
            doc_ref.delete()
        else:
            current_status = status or dispute.status
            doc_ref.set({
                'id': dispute.id,
                'task_id': dispute.task.id,
                'task_title': dispute.task.title,
                'raised_by': dispute.raised_by.username,
                'raised_by_id': dispute.raised_by.id,
                'posted_by_id': dispute.task.posted_by.id if dispute.task and dispute.task.posted_by else None,
                'reason': dispute.reason,
                'status': current_status,
                'created_at': dispute.created_at.isoformat() if hasattr(dispute, 'created_at') and dispute.created_at else None,
                'updated_at': firestore.SERVER_TIMESTAMP
            }, merge=True)
    except Exception as e:
        logger.error(f"Failed to sync dispute {getattr(dispute, 'id', None)} to Firestore: {e}")

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

        sync_dispute_to_firestore(dispute)
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

    sync_dispute_to_firestore(dispute, status='withdrawn', delete=True)
    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'. Your deposit bond has been refunded.")
    return redirect('my_tasks')
