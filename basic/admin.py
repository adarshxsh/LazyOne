from django.contrib import admin
from .models import UserProfile, Task, Dispute, RewardLedger, ReputationLog

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'reputation_score', 'risk_tier', 'tasks_completed', 'tasks_abandoned', 'disputes_won', 'disputes_lost', 'rewards')
    search_fields = ('user__username', 'first_name', 'last_name')
    list_filter = ('risk_tier',)

@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ('title', 'posted_by', 'taken_by', 'reward', 'poster_collateral', 'taker_collateral', 'status', 'created_at')
    search_fields = ('title', 'description', 'posted_by__username', 'taken_by__username')
    list_filter = ('status',)

@admin.register(Dispute)
class DisputeAdmin(admin.ModelAdmin):
    list_display = ('task', 'raised_by', 'status', 'created_at')
    list_filter = ('status',)

@admin.register(RewardLedger)
class RewardLedgerAdmin(admin.ModelAdmin):
    list_display = ('user', 'amount', 'transaction_type', 'description', 'created_at')
    list_filter = ('transaction_type',)

@admin.register(ReputationLog)
class ReputationLogAdmin(admin.ModelAdmin):
    list_display = ('user', 'change', 'new_score', 'reason', 'created_at', 'created_by')
    search_fields = ('user__username', 'reason')

