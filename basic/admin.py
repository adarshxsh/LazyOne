from django.contrib import admin
from .models import Dispute, DisputeAttachment

@admin.register(Dispute)
class DisputeAdmin(admin.ModelAdmin):
    list_display = ('id', 'task', 'raised_by', 'status', 'created_at')

@admin.register(DisputeAttachment)
class DisputeAttachmentAdmin(admin.ModelAdmin):
    list_display = ('id', 'dispute', 'uploaded_by', 'file_name', 'file_size', 'created_at')
