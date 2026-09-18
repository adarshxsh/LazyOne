from django.contrib import admin
from .models import UserProfile, Task, Dispute, DisputeVote, RewardLedger

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'rewards', 'college', 'hostel')

@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ('title', 'reward', 'posted_by', 'taken_by', 'status', 'created_at')

@admin.register(Dispute)
class DisputeAdmin(admin.ModelAdmin):
    list_display = ('task', 'raised_by', 'status', 'poster_escrow_status', 'worker_escrow_status', 'created_at')

@admin.register(DisputeVote)
class DisputeVoteAdmin(admin.ModelAdmin):
    list_display = ('dispute', 'voter', 'voted_for', 'stake_amount', 'created_at')

@admin.register(RewardLedger)
class RewardLedgerAdmin(admin.ModelAdmin):
    list_display = ('user', 'transaction_type', 'amount', 'task', 'created_at')

