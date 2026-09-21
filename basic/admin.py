from django.contrib import admin
from .models import Dispute, DisputeEvidence

@admin.register(Dispute)
class DisputeAdmin(admin.ModelAdmin):
    list_display = ('id', 'task', 'raised_by', 'status', 'escrow_status', 'created_at')
    list_filter = ('status', 'escrow_status')
    search_fields = ('task__title', 'raised_by__username', 'reason')

@admin.register(DisputeEvidence)
class DisputeEvidenceAdmin(admin.ModelAdmin):
    list_display = ('id', 'dispute', 'submitted_by', 'created_at')
    search_fields = ('dispute__task__title', 'submitted_by__username', 'description')

