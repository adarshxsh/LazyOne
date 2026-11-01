import functools

from django.db import IntegrityError
from rest_framework.response import Response

from .models import IdempotencyRecord


def idempotent(view):
    """Replay the stored response when a client retries a POST with the same Idempotency-Key."""
    @functools.wraps(view)
    def wrapper(self_or_request, *args, **kwargs):
        request = getattr(self_or_request, 'request', self_or_request)
        key = request.headers.get('Idempotency-Key')
        if not key or not request.user.is_authenticated:
            return view(self_or_request, *args, **kwargs)
        key = key[:100]
        existing = IdempotencyRecord.objects.filter(user=request.user, key=key).first()
        if existing:
            if existing.path != request.path or existing.method != request.method:
                return Response({'detail': 'Idempotency-Key was already used for a different request.'}, status=422)
            return Response(existing.body, status=existing.status_code, headers={'Idempotent-Replay': 'true'})
        response = view(self_or_request, *args, **kwargs)
        if response.status_code < 500:
            try:
                IdempotencyRecord.objects.create(user=request.user, key=key, method=request.method,
                                                 path=request.path, status_code=response.status_code,
                                                 body=response.data)
            except IntegrityError:
                pass
        return response
    return wrapper
