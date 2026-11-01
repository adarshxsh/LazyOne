import hmac

from django.conf import settings
from django.http import HttpResponseForbidden, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from ..services.jobs import run_due


@csrf_exempt
def cron(request):
    """Called every minute by Vercel Cron (sends `Authorization: Bearer $CRON_SECRET`)."""
    secret = settings.LAZYONE['CRON_SECRET']
    supplied = request.headers.get('Authorization', '').removeprefix('Bearer ').strip()
    if not secret or not hmac.compare_digest(secret, supplied):
        return HttpResponseForbidden()
    return JsonResponse(run_due())


def healthz(request):
    from django.db import connection
    with connection.cursor() as c:
        c.execute('SELECT 1')
    return JsonResponse({'ok': True})
