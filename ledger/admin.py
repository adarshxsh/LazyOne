from django.contrib import admin

from .models import Account, Entry, Transfer


class EntryInline(admin.TabularInline):
    model = Entry
    extra = 0
    can_delete = False
    readonly_fields = ('account', 'amount')


@admin.register(Transfer)
class TransferAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'reason', 'idempotency_key', 'task')
    list_filter = ('reason',)
    search_fields = ('idempotency_key',)
    inlines = [EntryInline]

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ('key', 'kind', 'balance')
    list_filter = ('kind',)
    search_fields = ('key',)
    readonly_fields = ('key', 'kind', 'user', 'balance')
