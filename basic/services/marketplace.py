"""
Task lifecycle (ARCHITECTURE_V2 §4.2). The only code allowed to change Task.status.

    apply(task, "accept", actor=user)

Every transition: locks the task row, looks up (status, action) in TRANSITIONS, checks the
actor, runs effects (ledger movements etc.) in the same transaction, records a TaskEvent,
and emits an outbox event for notifications/realtime after commit.
"""
from dataclasses import dataclass, field
from typing import Callable

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ledger import services as ledger
from ledger.models import Transfer
from outbox.services import emit

from ..models import Conversation, Task, TaskEvent

S = Task.Status


class TransitionError(Exception):
    """Raised when an action is not allowed; the message is safe to show to users."""


# ---------- actor checks ----------
def _poster(task, actor): return actor is not None and actor.pk == task.posted_by_id
def _taker(task, actor): return actor is not None and actor.pk == task.taken_by_id
def _party(task, actor): return _poster(task, actor) or _taker(task, actor)
def _not_poster(task, actor): return actor is not None and actor.pk != task.posted_by_id
def _system(task, actor): return actor is None


# ---------- effects (run inside the transaction) ----------
def _assign(task, actor, **kw):
    task.taken_by = actor
    task.cancellation_requested = False
    conv, _ = Conversation.objects.get_or_create(task=task)
    conv.participants.add(task.posted_by, actor)
    emit('conversation.members_changed', {'conversation_id': conv.pk})


def _unassign(task, actor, **kw):
    task.taken_by = None
    task.cancellation_requested = False
    task.submitted_at = None
    task.submission_note = ''
    task.opened_at = timezone.now()  # re-opened tasks start their waves again
    from .feed import schedule_wave_notifications
    schedule_wave_notifications(task)


def _submit(task, actor, note='', **kw):
    task.submitted_at = timezone.now()
    task.submission_note = (note or '')[:2000]


def _flag_cancel(task, actor, **kw):
    if task.cancellation_requested:
        raise TransitionError("Cancellation has already been requested.")
    task.cancellation_requested = True


def _require_cancel_flag(task, actor, **kw):
    if not task.cancellation_requested:
        raise TransitionError("There is no pending cancellation for this task.")
    task.cancellation_requested = False


def _require_auto_approve_due(task, actor, **kw):
    if not task.submitted_at or timezone.now() < task.submitted_at + settings.LAZYONE['AUTO_APPROVE_AFTER']:
        raise TransitionError("Auto-approval is not due yet.")


def _require_deadline_passed(task, actor, **kw):
    if not task.deadline or task.deadline > timezone.now():
        raise TransitionError("Deadline has not passed.")


def _payout(task, actor, **kw):
    ledger.transfer(
        [(ledger.task_escrow(task), -task.reward), (ledger.user_account(task.taken_by), task.reward)],
        key=f"task:{task.pk}:settle", reason=Transfer.Reason.TASK_PAYOUT, task=task,
        description=f"Completed task: '{task.title}'",
    )


def _refund(task, actor, **kw):
    ledger.transfer(
        [(ledger.task_escrow(task), -task.reward), (ledger.user_account(task.posted_by), task.reward)],
        key=f"task:{task.pk}:settle", reason=Transfer.Reason.TASK_REFUND, task=task,
        description=f"Refund for task: '{task.title}'",
    )


def _concede_dispute(task, actor, **kw):
    from . import disputes
    disputes.close_on_concession(task)


@dataclass(frozen=True)
class Rule:
    to: str
    actor: Callable
    effects: tuple = field(default_factory=tuple)
    deny_message: str = "You are not allowed to do that."


TRANSITIONS = {
    (S.AVAILABLE, 'accept'):          Rule(S.IN_PROGRESS, _not_poster, (_assign,), "You cannot take your own task."),
    (S.AVAILABLE, 'cancel'):          Rule(S.CANCELLED, _poster, (_refund,)),
    (S.AVAILABLE, 'expire'):          Rule(S.EXPIRED, _system, (_require_deadline_passed, _refund)),

    (S.IN_PROGRESS, 'submit'):        Rule(S.SUBMITTED, _taker, (_submit,)),
    (S.IN_PROGRESS, 'approve'):       Rule(S.COMPLETED, _poster, (_payout,)),
    (S.IN_PROGRESS, 'abandon'):       Rule(S.AVAILABLE, _taker, (_unassign,)),
    (S.IN_PROGRESS, 'request_cancel'): Rule(S.IN_PROGRESS, _poster, (_flag_cancel,)),
    (S.IN_PROGRESS, 'accept_cancel'): Rule(S.CANCELLED, _taker, (_require_cancel_flag, _refund)),
    (S.IN_PROGRESS, 'dispute'):       Rule(S.DISPUTED, _party),

    (S.SUBMITTED, 'approve'):         Rule(S.COMPLETED, _poster, (_payout,)),
    (S.SUBMITTED, 'auto_approve'):    Rule(S.COMPLETED, _system, (_require_auto_approve_due, _payout)),
    (S.SUBMITTED, 'dispute'):         Rule(S.DISPUTED, _party),

    (S.DISPUTED, 'approve'):          Rule(S.COMPLETED, _poster, (_payout, _concede_dispute)),
    (S.DISPUTED, 'withdraw_dispute'): Rule(S.IN_PROGRESS, _party),
    (S.DISPUTED, 'verdict_taker'):    Rule(S.COMPLETED, _system, (_payout,)),
    (S.DISPUTED, 'verdict_poster'):   Rule(S.CANCELLED, _system, (_refund,)),
}


def allowed_actions(task, actor):
    return [action for (status, action), rule in TRANSITIONS.items()
            if status == task.status and rule.actor(task, actor)]


def apply(task, action, actor=None, *, note=''):
    """Apply `action` to `task` (instance or pk). Returns the updated task."""
    pk = getattr(task, 'pk', task)
    with transaction.atomic():
        task = Task.objects.select_for_update(of=('self',)).select_related('posted_by', 'taken_by').filter(pk=pk).first()
        if task is None:
            raise TransitionError("Task not found.")
        rule = TRANSITIONS.get((task.status, action))
        if rule is None:
            raise TransitionError(f"Cannot {action.replace('_', ' ')} a task that is {task.get_status_display().lower()}.")
        if not rule.actor(task, actor):
            raise TransitionError(rule.deny_message)

        from_status = task.status
        for effect in rule.effects:
            effect(task, actor, note=note)
        task.status = rule.to
        task.version += 1
        task.save()
        TaskEvent.objects.create(task=task, action=action, actor=actor,
                                 from_status=from_status, to_status=task.status, note=note[:500])
        emit('task.transitioned', {'task_id': task.pk, 'action': action,
                                   'actor_id': getattr(actor, 'pk', None), 'from': from_status, 'to': task.status})
    return task


def post_task(user, *, title, description, reward, deadline=None, wave_policy=Task.WavePolicy.DEFAULT):
    """Create a task and fund its escrow from the poster's wallet in one transaction."""
    title = (title or '').strip()
    if not title:
        raise TransitionError("Title is required.")
    if not isinstance(reward, int) or reward <= 0:
        raise TransitionError("Reward must be a positive number.")
    if deadline is not None and deadline <= timezone.now():
        raise TransitionError("Deadline must be in the future.")
    if wave_policy not in Task.WavePolicy.values:
        raise TransitionError("Unknown visibility option.")

    profile = user.userprofile
    with transaction.atomic():
        task = Task.objects.create(
            title=title[:200], description=description or '', reward=reward, posted_by=user,
            deadline=deadline, status=S.AVAILABLE, community=profile.get_community(),
            wave_policy=wave_policy, opened_at=timezone.now(),
        )
        try:
            ledger.transfer(
                [(ledger.user_account(user), -reward), (ledger.task_escrow(task), reward)],
                key=f"task:{task.pk}:fund", reason=Transfer.Reason.TASK_FUND, task=task,
                description=f"Reserved for task: '{task.title}'",
            )
        except ledger.InsufficientFunds:
            raise TransitionError(f"You only have {ledger.balance_of(user)} points, not enough to offer this reward.")
        TaskEvent.objects.create(task=task, action='post', actor=user, from_status='', to_status=task.status)
        from .feed import schedule_wave_notifications
        schedule_wave_notifications(task)
    return task
