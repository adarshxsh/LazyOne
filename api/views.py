"""REST API v1 for the Flutter app and future SPA (ARCHITECTURE_V2 §6, Phase 3)."""
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from basic.models import Conversation, Dispute, Task
from basic.realtime import create_custom_token
from basic.services import disputes, feed, marketplace, messaging
from basic.services.marketplace import TransitionError
from basic.services.permissions import ChatService
from ledger import services as ledger

from .idempotency import idempotent
from .serializers import DisputeSerializer, TaskCreateSerializer, TaskSerializer

TASK_ACTIONS = {'accept', 'submit', 'approve', 'cancel', 'abandon', 'request_cancel', 'accept_cancel'}


class PostTaskThrottle(ScopedRateThrottle):
    scope_attr = 'throttle_scope'


def _error(e, code=status.HTTP_409_CONFLICT):
    return Response({'detail': str(e)}, status=code)


def _task_qs():
    return Task.objects.select_related('posted_by', 'taken_by', 'conversation')


def _visible_task(user, public_id):
    task = get_object_or_404(_task_qs(), public_id=public_id)
    if user.pk in (task.posted_by_id, task.taken_by_id) or feed.can_see(user, task):
        return task
    if task.disputes.filter(jurors__juror=user).exists() or user.is_staff:
        return task
    return None


@api_view(['GET'])
def me(request):
    profile = request.user.userprofile
    community = profile.get_community()
    return Response({
        'id': request.user.pk, 'username': request.user.username, 'email': request.user.email,
        'balance': ledger.balance_of(request.user),
        'community': {'slug': community.slug, 'name': community.name},
        'phone_verified': profile.is_phone_verified,
    })


@api_view(['GET'])
def task_feed(request):
    tasks = feed.feed_for(request.user, request.query_params.get('q'), limit=50)
    return Response(TaskSerializer(tasks, many=True, context={'request': request}).data)


@api_view(['GET'])
def my_tasks(request):
    tasks = _task_qs().filter(Q(posted_by=request.user) | Q(taken_by=request.user)).order_by('-created_at')[:100]
    return Response(TaskSerializer(tasks, many=True, context={'request': request}).data)


@api_view(['POST'])
@throttle_classes([ScopedRateThrottle])
@idempotent
def create_task(request):
    s = TaskCreateSerializer(data=request.data)
    s.is_valid(raise_exception=True)
    try:
        task = marketplace.post_task(request.user, **s.validated_data)
    except TransitionError as e:
        return _error(e, status.HTTP_400_BAD_REQUEST)
    return Response(TaskSerializer(task, context={'request': request}).data, status=status.HTTP_201_CREATED)


create_task.cls.throttle_scope = 'post_task'


@api_view(['GET'])
def task_detail(request, public_id):
    task = _visible_task(request.user, public_id)
    if task is None:
        return Response({'detail': 'Not found.'}, status=404)
    return Response(TaskSerializer(task, context={'request': request}).data)


@api_view(['POST'])
@idempotent
def task_action(request, public_id, action):
    if action not in TASK_ACTIONS:
        return Response({'detail': 'Unknown action.'}, status=404)
    task = _visible_task(request.user, public_id)
    if task is None:
        return Response({'detail': 'Not found.'}, status=404)
    expected = request.data.get('version')
    if expected is not None and str(expected) != str(task.version):
        return Response({'detail': 'Task changed since you loaded it.', 'version': task.version}, status=409)
    try:
        task = marketplace.apply(task, action, request.user, note=str(request.data.get('note', '')))
    except TransitionError as e:
        return _error(e)
    task = _task_qs().get(pk=task.pk)
    return Response(TaskSerializer(task, context={'request': request}).data)


@api_view(['GET'])
def task_events(request, public_id):
    task = get_object_or_404(Task, public_id=public_id)
    if request.user.pk not in (task.posted_by_id, task.taken_by_id) and not request.user.is_staff:
        return Response({'detail': 'Not found.'}, status=404)
    return Response([{'action': e.action, 'from': e.from_status, 'to': e.to_status,
                      'actor': e.actor.username if e.actor else None, 'note': e.note, 'at': e.created_at}
                     for e in task.events.select_related('actor')])


@api_view(['POST'])
@idempotent
def raise_dispute(request, public_id):
    task = get_object_or_404(Task, public_id=public_id)
    try:
        d = disputes.raise_dispute(task, request.user, request.data.get('reason'),
                                   request.data.get('dispute_type') or Dispute.DisputeType.OTHER)
    except TransitionError as e:
        return _error(e)
    return Response(DisputeSerializer(d, context={'request': request}).data, status=201)


def _dispute_for(request, public_id):
    d = get_object_or_404(Dispute.objects.select_related('task'), public_id=public_id)
    return d if disputes.can_view(request.user, d) else None


@api_view(['GET'])
def dispute_detail(request, public_id):
    d = _dispute_for(request, public_id)
    if d is None:
        return Response({'detail': 'Not found.'}, status=404)
    return Response(DisputeSerializer(d, context={'request': request}).data)


@api_view(['POST'])
@idempotent
def dispute_action(request, public_id, action):
    d = _dispute_for(request, public_id)
    if d is None:
        return Response({'detail': 'Not found.'}, status=404)
    try:
        if action == 'evidence':
            disputes.add_evidence(d, request.user, request.data.get('text'))
        elif action == 'vote':
            disputes.cast_vote(d, request.user, request.data.get('vote'))
        elif action == 'withdraw':
            disputes.withdraw(d, request.user)
        else:
            return Response({'detail': 'Unknown action.'}, status=404)
    except TransitionError as e:
        return _error(e)
    d.refresh_from_db()
    return Response(DisputeSerializer(d, context={'request': request}).data)


@api_view(['GET', 'POST'])
@throttle_classes([ScopedRateThrottle])
def conversation_messages(request, public_id):
    conv = get_object_or_404(Conversation, public_id=public_id)
    if request.method == 'GET':
        if not ChatService.can_read(request.user, conv):
            return Response({'detail': 'Not found.'}, status=404)
        try:
            after = int(request.query_params.get('after') or 0) or None
        except ValueError:
            after = None
        return Response(messaging.history(conv, after_id=after))
    try:
        msg = messaging.send(conv, request.user, request.data.get('content'))
    except PermissionError:
        return Response({'detail': 'Not allowed.'}, status=403)
    except TransitionError as e:
        return _error(e, status.HTTP_400_BAD_REQUEST)
    return Response(messaging.serialize(msg), status=201)


conversation_messages.cls.throttle_scope = 'messages'


@api_view(['GET'])
def realtime_token(request):
    token = create_custom_token(request.user)
    if token is None:
        return Response({'detail': 'Realtime is not configured.'}, status=503)
    return Response({'token': token, 'uid': f"django:{request.user.pk}"})


@api_view(['GET'])
def ledger_history(request):
    from ledger.models import Entry
    acc = ledger.user_account(request.user)
    entries = Entry.objects.filter(account=acc).select_related('transfer', 'transfer__task').order_by('-id')[:100]
    return Response({'balance': acc.balance, 'entries': [
        {'amount': e.amount, 'reason': e.transfer.reason, 'description': e.transfer.description,
         'task': e.transfer.task.public_id if e.transfer.task else None, 'at': e.transfer.created_at}
        for e in entries]})


__all__ = ['permission_classes', 'IsAuthenticated']
