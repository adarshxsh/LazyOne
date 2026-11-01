from django.conf import settings
from django.db import models


class IdempotencyRecord(models.Model):
    """Stored response for a (user, Idempotency-Key) so client retries are safe."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    key = models.CharField(max_length=100)
    method = models.CharField(max_length=10)
    path = models.CharField(max_length=300)
    status_code = models.PositiveSmallIntegerField()
    body = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'key'], name='unique_idempotency_key_per_user')]
