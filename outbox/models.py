from django.db import models
from django.utils import timezone


class OutboxEvent(models.Model):
    """A side effect (push, Firestore write, notification) recorded in the same DB
    transaction as the change that caused it, and executed after commit."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        DONE = 'done', 'Done'
        FAILED = 'failed', 'Failed (gave up)'

    topic = models.CharField(max_length=64, db_index=True)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=['status', 'available_at'])]

    def __str__(self):
        return f"{self.topic} #{self.pk} ({self.status})"
