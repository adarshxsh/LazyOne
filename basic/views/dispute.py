from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from ..models import Dispute
from django.views.decorators.http import require_POST

@login_required(login_url='/login/')
def dispute_detail_view(request, dispute_id):
    """
    Displays the details of a specific dispute.
    """
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    # Authorization: Ensure the user is part of the disputed task
    if request.user != task.posted_by and request.user != task.taken_by and not request.user.is_staff:
        messages.error(request, "You are not authorized to view this dispute.")
        return redirect('home')

    context = {
        'dispute': dispute,
        'task': task
    }
    return render(request, 'dispute_detail.html', context)

@login_required(login_url='/login/')
@require_POST # Ensures this view only accepts POST requests
def withdraw_dispute(request, dispute_id):
    """
    Allows the user who raised a dispute to withdraw it.
    """
    dispute = get_object_or_404(Dispute, id=dispute_id)
    task = dispute.task

    # Authorization: Only the user who raised the dispute can withdraw it.
    if request.user != dispute.raised_by:
        messages.error(request, "You are not authorized to perform this action.")
        return redirect('my_tasks')

    # Revert task status and delete the dispute
    task.status = 'in_progress'
    task.save()
    dispute.delete()

    messages.success(request, f"You have successfully withdrawn the dispute for '{task.title}'.")
    return redirect('my_tasks')