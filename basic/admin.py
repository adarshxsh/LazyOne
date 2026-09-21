from django.contrib import admin
from .models import Dispute, DisputeAuditEvent

admin.site.register(Dispute)
admin.site.register(DisputeAuditEvent)
