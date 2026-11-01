"""
Per-user rate limits for web (non-API) POST endpoints. The API uses DRF throttles.

Counts live in the Django cache, so in production CACHES must be shared (Redis via
REDIS_URL); with the per-process default cache each serverless instance counts alone.
"""
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import redirect

WINDOWS = {'minute': 60, 'hour': 3600, 'day': 86400}


def ratelimit(name, rate):
    """`rate` like '30/hour'. Applies to POST requests from logged-in users only."""
    count, per = rate.split('/')
    limit, window = int(count), WINDOWS[per]

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method == 'POST' and request.user.is_authenticated and not getattr(settings, 'RATELIMIT_DISABLED', False):
                key = f"rl:{name}:{request.user.pk}"
                cache.add(key, 0, window)
                try:
                    used = cache.incr(key)
                except ValueError:  # expired between add and incr
                    cache.set(key, 1, window)
                    used = 1
                if used > limit:
                    msg = "You're doing that too often. Please wait a bit and try again."
                    wants_json = (request.content_type == 'application/json'
                                  or 'application/json' in request.headers.get('Accept', ''))
                    if wants_json:
                        return JsonResponse({'success': False, 'error': msg, 'detail': msg}, status=429)
                    messages.error(request, msg)
                    return redirect(request.META.get('HTTP_REFERER') or 'home')
            return view(request, *args, **kwargs)
        return wrapped
    return decorator
