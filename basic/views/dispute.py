from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from ..models import Dispute
from django.views.decorators.http import require_POST
from django.db import transaction
from ..services.permissions import DisputeService

@login_required(login_url='/login/')
def dispute_detail_view(request, public_id):
    """
    Displays the details of a specific dispute.
    """
    dispute = get_object_or_404(Dispute, public_id=public_id)
    task = dispute.task

    # Authorization: Use DisputeService
    if not DisputeService.can_view(request.user, dispute):
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    context = {
        'dispute': dispute,
        'task': task
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
@require_POST # Ensures this view only accepts POST requests
def withdraw_dispute(request, public_id):
    """
    Allows the user who raised a dispute to withdraw it.
    """
    dispute = get_object_or_404(Dispute, public_id=public_id)
    task = dispute.task

    # Authorization: Use DisputeService
    if not DisputeService.can_withdraw(request.user, dispute):
        messages.error(request, "You are not authorized to perform this action.")
        return redirect('my_tasks')

    # Revert task status and mark dispute withdrawn
    with transaction.atomic():
        task.status = 'in_progress'
        task.save()
        dispute.status = 'withdrawn'
        dispute.save()

    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'.")
    return redirect('my_tasks')