from django.contrib import admin

from .models import OutboxEvent


@admin.register(OutboxEvent)
class OutboxEventAdmin(admin.ModelAdmin):
    list_display = ('pk', 'topic', 'status', 'attempts', 'available_at', 'processed_at')
    list_filter = ('status', 'topic')
    readonly_fields = [f.name for f in OutboxEvent._meta.fields]
