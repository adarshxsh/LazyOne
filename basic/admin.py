from django.contrib import admin
from .models import Dispute, DisputeEvidence

@admin.register(Dispute)
class DisputeAdmin(admin.ModelAdmin):
    list_display = ('id', 'task', 'raised_by', 'status', 'deposit_amount', 'created_at')
    list_filter = ('status', 'escrow_status')
    search_fields = ('task__title', 'raised_by__username', 'reason')

@admin.register(DisputeEvidence)
class DisputeEvidenceAdmin(admin.ModelAdmin):
    list_display = ('id', 'dispute', 'uploaded_by', 'original_filename', 'uploaded_at')
    list_filter = ('uploaded_at',)
    search_fields = ('dispute__task__title', 'uploaded_by__username', 'caption', 'original_filename')

