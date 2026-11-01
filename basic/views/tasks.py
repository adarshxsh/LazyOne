"""Thin web views over services.marketplace / services.disputes (ARCHITECTURE_V2 §3)."""
from datetime import datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..models import Dispute, Task
from ..services import disputes, marketplace
from ..services.marketplace import TransitionError
from ..ratelimit import ratelimit


def _default_deadline():
    return (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')


def _render_add(request, status=200):
    return render(request, 'add_task.html', {
        'default_deadline': _default_deadline(),
        'wave_policies': Task.WavePolicy.choices,
    }, status=status)


@login_required(login_url='/login/')
@ratelimit('post_task', '30/hour')
def add_task(request):
    if request.method != 'POST':
        return _render_add(request)
    try:
        reward = int(request.POST.get('reward', ''))
        deadline = timezone.make_aware(datetime.strptime(request.POST.get('deadline', ''), '%Y-%m-%dT%H:%M'))
    except (ValueError, TypeError):
        messages.error(request, 'Invalid reward amount or deadline format.')
        return _render_add(request)
    try:
        task = marketplace.post_task(
            request.user, title=request.POST.get('title'), description=request.POST.get('description'),
            reward=reward, deadline=deadline,
            wave_policy=request.POST.get('wave_policy') or Task.WavePolicy.DEFAULT,
        )
    except TransitionError as e:
        messages.error(request, str(e))
        return _render_add(request)
    messages.success(request, f'Task added successfully! {task.reward} points have been reserved.')
    return redirect('home')


def _transition_view(action, success):
    @login_required(login_url='/login/')
    @require_POST
    def view(request, public_id):
        task = get_object_or_404(Task, public_id=public_id)
        try:
            marketplace.apply(task, action, request.user, note=request.POST.get('note', ''))
        except TransitionError as e:
            messages.error(request, str(e))
        else:
            messages.success(request, success)
        return redirect('my_tasks')
    view.__name__ = f"{action}_view"
    return view


take_task = _transition_view('accept', "Task has been assigned to you. A chat has been created.")
submit_task = _transition_view('submit', "Submitted! The poster has 48h to approve before it auto-approves.")
complete_task = _transition_view('approve', "Task marked as complete and the reward was paid.")
cancel_task = _transition_view('cancel', "You have cancelled the task and your points have been refunded.")
request_cancellation = _transition_view('request_cancel', "A cancellation request has been sent to the task taker.")
accept_cancellation = _transition_view('accept_cancel', "You have accepted the cancellation.")
abandon_task = _transition_view('abandon', "You have abandoned the task. It is now available for others.")


@login_required(login_url='/login/')
@ratelimit('raise_dispute', '10/hour')
def raise_dispute(request, public_id):
    task = get_object_or_404(Task, public_id=public_id)
    existing = task.disputes.filter(status__in=[Dispute.Status.OPEN, Dispute.Status.ESCALATED]).first()
    if existing:
        return redirect('dispute_detail', public_id=existing.public_id)
    if request.method != 'POST':
        return redirect('my_tasks')  # the modal is handled client-side
    try:
        dispute = disputes.raise_dispute(task, request.user, request.POST.get('reason'),
                                         request.POST.get('dispute_type') or Dispute.DisputeType.OTHER,
                                         taker_pct=request.POST.get('taker_pct') or None)
    except TransitionError as e:
        messages.error(request, str(e))
        return redirect('my_tasks')
    messages.success(request, f"Dispute raised. The other side has 24 h to settle; a refundable bond of "
                              f"{dispute.bond_amount} points is held.")
    return redirect('dispute_detail', public_id=dispute.public_id)


@login_required(login_url='/login/')
def my_tasks(request):
    posted_tasks = Task.objects.filter(posted_by=request.user).select_related('taken_by', 'conversation').order_by('-created_at')
    taken_tasks = Task.objects.filter(taken_by=request.user).select_related('posted_by', 'conversation').order_by('-created_at')
    jury_duties = (request.user.jury_duties.select_related('dispute__task')
                   .filter(dispute__status=Dispute.Status.OPEN, dispute__stage__in=['jury', 'appeal'])
                   .order_by('dispute__voting_deadline'))
    from ..services import prestige
    score = prestige.of(request.user)
    context = {'posted_tasks': posted_tasks, 'taken_tasks': taken_tasks, 'jury_duties': jury_duties,
               'dispute_types': Dispute.DisputeType.choices, 'my_prestige': score, 'my_tier': prestige.tier(score)}
    return render(request, 'my_tasks.html', context)
