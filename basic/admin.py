from django.contrib import admin
from .models import UserProfile, Task, RewardLedger, Dispute

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'rewards', 'batch')

@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ('title', 'posted_by', 'taken_by', 'reward', 'status', 'created_at')

@admin.register(RewardLedger)
class RewardLedgerAdmin(admin.ModelAdmin):
    list_display = ('user', 'task', 'amount', 'transaction_type', 'created_at')

@admin.register(Dispute)
class DisputeAdmin(admin.ModelAdmin):
    list_display = ('task', 'raised_by', 'status', 'poster_amount', 'doer_amount', 'resolved_by', 'created_at')

