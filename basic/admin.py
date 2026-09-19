from django.contrib import admin
from .models import Dispute, DisputeEvidence

@admin.register(Dispute)
class DisputeAdmin(admin.ModelAdmin):
    list_display = ('task', 'raised_by', 'status', 'evidence_deadline', 'expires_at', 'created_at')

@admin.register(DisputeEvidence)
class DisputeEvidenceAdmin(admin.ModelAdmin):
    list_display = ('dispute', 'submitted_by', 'created_at')

