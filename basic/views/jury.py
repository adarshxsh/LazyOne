from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from ..models import Dispute, JuryAssignment, JuryPool, DisputeVote
from ..jury_utils import cast_juror_vote, check_and_aggregate_dispute, sync_jury_pool


@login_required(login_url='/login/')
def jury_dashboard_view(request):
    sync_jury_pool()
    jury_pool, _ = JuryPool.objects.get_or_create(user=request.user)
    assignments = JuryAssignment.objects.filter(juror=request.user).select_related('dispute', 'dispute__task').order_by('-assigned_at')

    for assignment in assignments:
        check_and_aggregate_dispute(assignment.dispute)

    pending_assignments = [a for a in assignments if not a.has_voted and a.dispute.status == 'open']
    completed_assignments = [a for a in assignments if a.has_voted or a.dispute.status == 'resolved']

    context = {
        'jury_pool': jury_pool,
        'pending_assignments': pending_assignments,
        'completed_assignments': completed_assignments,
    }
    return render(request, 'jury_dashboard.html', context)


@login_required(login_url='/login/')
def jury_dispute_detail_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    assignment = get_object_or_404(JuryAssignment, dispute=dispute, juror=request.user)
    check_and_aggregate_dispute(dispute)

    vote = DisputeVote.objects.filter(dispute=dispute, juror=request.user).first()
    task = dispute.task
    conversation = getattr(task, 'conversation', None)
    messages_list = conversation.messages.all() if conversation else []

    show_results = (dispute.status == 'resolved')

    context = {
        'dispute': dispute,
        'task': task,
        'assignment': assignment,
        'vote': vote,
        'messages_list': messages_list,
        'show_results': show_results,
    }
    return render(request, 'jury_dispute_detail.html', context)


@login_required(login_url='/login/')
@require_POST
def submit_jury_vote_view(request, dispute_id):
    dispute = get_object_or_404(Dispute, id=dispute_id)
    vote_choice = request.POST.get('vote')
    rationale = request.POST.get('rationale', '')

    if vote_choice not in ['poster', 'taker']:
        messages.error(request, "Invalid vote choice selected.")
        return redirect('jury_dispute_detail', dispute_id=dispute.id)

    success = cast_juror_vote(request.user, dispute, vote_choice, rationale)
    if success:
        messages.success(request, "Your vote has been cast confidentially. Thank you for your service!")
    else:
        messages.error(request, "Unable to cast vote. You may have already voted or the dispute is closed.")

    return redirect('jury_dispute_detail', dispute_id=dispute.id)
