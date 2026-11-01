"""
Transactional outbox (ARCHITECTURE_V2 §3 rule 4, §4.4).

    emit("message.created", {"message_id": 1})                # run right after commit
    emit("task.wave", {...}, delay=timedelta(minutes=10))      # run later by the job runner

Handlers register with @handler("topic"). A failing handler leaves the event pending
with exponential backoff; `manage.py run_jobs` (or the cron endpoint) retries it.
"""
import logging
from datetime import timedelta

from django.db import connection, transaction
from django.utils import timezone

from .models import OutboxEvent

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
_HANDLERS = {}


def handler(topic):
    def register(fn):
        _HANDLERS[topic] = fn
        return fn
    return register


def emit(topic, payload, *, delay=None):
    event = OutboxEvent.objects.create(
        topic=topic, payload=payload,
        available_at=timezone.now() + (delay or timedelta(0)),
    )
    if not delay:
        transaction.on_commit(lambda: process_one(event.pk))
    return event


def _run(event):
    _load_handlers()
    fn = _HANDLERS.get(event.topic)
    try:
        if fn is not None:
            fn(event.payload)
        event.status = OutboxEvent.Status.DONE
        event.processed_at = timezone.now()
        event.last_error = ''
    except Exception as exc:  # noqa: BLE001 - any handler failure is retried
        logger.exception("Outbox handler failed for %s #%s", event.topic, event.pk)
        event.attempts += 1
        event.last_error = repr(exc)[:2000]
        if event.attempts >= MAX_ATTEMPTS:
            event.status = OutboxEvent.Status.FAILED
        else:
            event.available_at = timezone.now() + timedelta(seconds=min(3600, 5 * 2 ** event.attempts))
    event.save()


def _lock_qs():
    qs = OutboxEvent.objects.select_for_update()
    if connection.features.has_select_for_update_skip_locked:
        qs = OutboxEvent.objects.select_for_update(skip_locked=True)
    return qs


def process_one(event_id):
    """Best-effort immediate delivery after commit. Never raises into the request."""
    try:
        with transaction.atomic():
            event = _lock_qs().filter(pk=event_id, status=OutboxEvent.Status.PENDING,
                                      available_at__lte=timezone.now()).first()
            if event:
                _run(event)
    except Exception:  # noqa: BLE001
        logger.exception("Immediate outbox processing failed for #%s", event_id)


def process_due(limit=200):
    """Process due events; returns how many were handled."""
    done = 0
    ids = list(OutboxEvent.objects.filter(status=OutboxEvent.Status.PENDING, available_at__lte=timezone.now())
               .order_by('available_at', 'pk').values_list('pk', flat=True)[:limit])
    for pk in ids:
        process_one(pk)
        done += 1
    return done


_loaded = False


def _load_handlers():
    global _loaded
    if not _loaded:
        import basic.handlers  # noqa: F401  registers handlers
        _loaded = True
