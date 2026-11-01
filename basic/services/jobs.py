"""Time-based work. Run every minute via `manage.py run_jobs` or the cron endpoint."""
import logging

from django.conf import settings
from django.utils import timezone

from outbox.services import process_due

from ..models import Task
from . import disputes, marketplace

logger = logging.getLogger(__name__)


def _each(qs, action):
    n = 0
    for pk in qs.values_list('pk', flat=True):
        try:
            marketplace.apply(pk, action, None)
            n += 1
        except marketplace.TransitionError:
            pass  # state changed concurrently; fine
        except Exception:  # noqa: BLE001 - one bad task must not stop the run
            logger.exception("Job %s failed for task %s", action, pk)
    return n


def run_due():
    now = timezone.now()
    report = {
        'expired': _each(Task.objects.filter(status=Task.Status.AVAILABLE, deadline__lte=now), 'expire'),
        'auto_approved': _each(Task.objects.filter(
            status=Task.Status.SUBMITTED, submitted_at__lte=now - settings.LAZYONE['AUTO_APPROVE_AFTER']), 'auto_approve'),
        'disputes_tallied': disputes.close_due_disputes(),
    }
    report['outbox_processed'] = process_due()
    return report
